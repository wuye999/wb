"""官方商品快照的离线协议、数据契约和命令回归；仅使用合成数据。"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from itertools import count
import io
import json
from pathlib import Path
import tempfile
from threading import Barrier, Lock
import unittest
from unittest.mock import Mock, patch

from sellerops.core.errors import AuthenticationError, NetworkError, PlatformApiError, RateLimitError
from sellerops.features.catalog import products, snapshot_open_api as snapshot
from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.integrations.wildberries.open_api import WBOpenAPI
from sellerops.settings import paths


def card(nm=101, vc='BCS-TEST-100', specs=(11, 12)):
    return {'nmID': nm, 'imtID': 201, 'nmUUID': 'fixture', 'vendorCode': vc,
            'title': '测试商品', 'brand': '品牌', 'subjectID': 20, 'subjectName': '类目',
            'createdAt': '2026-10-01T00:00:00Z', 'updatedAt': '2026-11-01T00:00:00Z',
            'dimensions': {'length': 10, 'width': 2, 'height': 3, 'weightBrutto': .25},
            'photos': [{'big': 'https://example.invalid/photo.webp'}],
            'sizes': [{'chrtID': chrt, 'techSize': str(chrt), 'skus': [f'barcode-{chrt}']}
                      for chrt in specs]}


def price(nm=101, vc='BCS-TEST-100'):
    return {'nmID': nm, 'vendorCode': vc, 'discount': 0, 'clubDiscount': 5,
            'currencyIsoCode4217': 'CNY', 'editableSizePrice': True,
            'sizes': [{'sizeID': 12, 'price': 20, 'discountedPrice': 20, 'clubDiscountedPrice': 19},
                      {'sizeID': 11, 'price': 0, 'discountedPrice': 0, 'clubDiscountedPrice': 0}]}


WAREHOUSE = {'id': 42, 'name': '莫斯科仓库'}


class TestSnapshotProtocol(unittest.TestCase):
    def client(self):
        api = WBOpenAPI('fixture-secret', 'cn')
        self.addCleanup(api.close)
        return api

    def test_cards_complete_cursor_pagination_with_no_photo_filter(self):
        api = self.client()
        pages = [{'cards': [card()], 'cursor': {'total': 1, 'updatedAt': 'one', 'nmID': 101}},
                 {'cards': [card(102, 'VC2', (13,))],
                  'cursor': {'total': 1, 'updatedAt': 'two', 'nmID': 102}},
                 {'cards': [], 'cursor': {'total': 0}}]
        with patch.object(api, '_request', side_effect=pages) as request:
            result = api.cards(page_size=1)
        self.assertEqual([row['nmID'] for row in result], [101, 102])
        bodies = [call.kwargs['json']['settings'] for call in request.call_args_list]
        self.assertEqual(bodies[0]['filter'], {'withPhoto': -1})
        self.assertEqual(bodies[1]['cursor'], {'limit': 1, 'updatedAt': 'one', 'nmID': 101})
        self.assertEqual(bodies[2]['cursor']['nmID'], 102)

    def test_incomplete_duplicate_missing_cursor_and_page_cap_fail(self):
        api = self.client()
        bad_pages = [
            [{'cards': []}], [{'cards': [], 'cursor': {'total': 1}}],
            [{'cards': [card()], 'cursor': {'total': 1}}],
            [{'cards': [card()], 'cursor': {'total': 1, 'updatedAt': 'x', 'nmID': 101}}] * 2,
            [{'cards': [card()], 'cursor': {'total': 1, 'updatedAt': 'x', 'nmID': 101}},
             {'cards': [card(102)], 'cursor': {'total': 1, 'updatedAt': 'x', 'nmID': 101}}],
        ]
        for pages in bad_pages:
            with self.subTest(pages=pages), patch.object(api, '_request', side_effect=pages):
                with self.assertRaises(PlatformApiError):
                    api.cards(page_size=1)
        with patch.object(api, '_request', return_value={
            'cards': [card()], 'cursor': {'total': 1, 'updatedAt': 'x', 'nmID': 101},
        }), self.assertRaisesRegex(PlatformApiError, '上限'):
            api.cards(page_size=1, max_pages=1)

    def test_prices_and_stocks_split_at_1000_and_validate_returned_ids(self):
        api = self.client()
        def response(service, method, path, **kwargs):
            self.assertEqual(method, 'POST')
            body = kwargs['json']
            batch = body.get('nmList', body.get('chrtIds'))
            self.assertLessEqual(len(batch), 1000)
            if 'nmList' in body:
                return {'data': {'listGoods': [{'nmID': nm, 'sizes': []} for nm in batch]}}
            self.assertEqual(path, '/api/v3/stocks/42')
            return {'stocks': [{'chrtId': chrt, 'amount': 0} for chrt in batch]}
        with patch.object(api, '_request', side_effect=response) as request:
            self.assertEqual(len(api.prices(list(range(1, 1002)))), 1001)
            self.assertEqual(len(api.stocks(42, list(range(1, 1002)))), 1001)
            self.assertEqual(request.call_count, 4)
        for value in ({}, {'data': {}}, {'data': {'listGoods': [{'nmID': 999}]}}):
            with patch.object(api, '_request', return_value=value), self.assertRaises(PlatformApiError):
                api.prices([1])
        for value in ({}, {'stocks': [{'chrtId': 999, 'amount': 0}]},
                      {'stocks': [{'chrtId': 1, 'amount': -1}]},
                      {'stocks': [{'chrtId': 1, 'amount': 0}] * 2}):
            with patch.object(api, '_request', return_value=value), self.assertRaises(PlatformApiError):
                api.stocks(42, [1])
        with patch.object(api, '_request') as request:
            self.assertEqual(api.prices([]), [])
            self.assertEqual(api.stocks(42, []), [])
            request.assert_not_called()
            with self.assertRaises(ValueError):
                api.stocks(True, [1])

    def test_read_routes_auth_errors_and_bounded_504_network_retries(self):
        api = self.client()
        success = Mock(status_code=200)
        success.json.return_value = {'stocks': []}
        with patch.object(api.session, 'request', return_value=success) as request:
            self.assertEqual(api.stocks(42, [11]), [])
            call = request.call_args
            self.assertEqual(call.args[:2], ('POST', 'https://marketplace-api.wildberries.cn/api/v3/stocks/42'))
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertNotIn('Cookie', api.session.headers)
        for method in ('PUT', 'DELETE'):
            with self.assertRaises(ValueError):
                api._request('marketplace-api', method, '/api/v3/stocks/42')
        for status, error in ((401, AuthenticationError), (403, AuthenticationError), (429, RateLimitError)):
            with patch.object(api.session, 'request', return_value=Mock(status_code=status)), \
                    patch.object(api, '_last', {}), self.assertRaises(error):
                api.stocks(42, [11])
        with patch.object(api.session, 'request', side_effect=[Mock(status_code=504), success]) as request, \
                patch('sellerops.integrations.wildberries.open_api.time.sleep'), \
                patch('sellerops.integrations.wildberries.open_api.time.monotonic', side_effect=count()):
            api._last.clear()
            self.assertEqual(api.stocks(42, [11]), [])
            self.assertEqual(request.call_count, 2)
        import requests
        with patch.object(api.session, 'request', side_effect=requests.ConnectionError('fixture-secret')) as request, \
                patch('sellerops.integrations.wildberries.open_api.time.sleep'), \
                patch('sellerops.integrations.wildberries.open_api.time.monotonic', side_effect=count()):
            api._last.clear()
            with self.assertRaises(NetworkError) as raised:
                api.stocks(42, [11])
            self.assertEqual(request.call_count, 3)
            self.assertNotIn('fixture-secret', str(raised.exception))


class TestSnapshotNormalization(unittest.TestCase):
    def test_multisize_prices_join_by_id_zero_and_missing_are_distinct(self):
        rows, stats = snapshot.normalize_rows(1, [card()], [price()], [{'chrtId': 11, 'amount': 0}], WAREHOUSE)
        row = rows[0]
        self.assertEqual([size['price'] for size in row['sizeList']], [0, 20])
        self.assertEqual(row['sizeList'][0]['stockList'], [{'warehouseId': 42, 'amount': 0}])
        self.assertEqual(row['sizeList'][1]['stockList'], [])
        self.assertEqual(row['discount'], 0)
        self.assertEqual(row['clubDiscount'], 5)
        self.assertEqual(row['createAt'], card()['createdAt'])
        self.assertEqual(row['dimensionsWeightBrutto'], .25)
        self.assertEqual(row['currencyIsoCode'], 'CNY')
        self.assertEqual(stats, {'missingPriceSpecs': 0, 'missingStockSpecs': 1, 'missingImages': 0})
        raw = card()
        raw.pop('photos')
        rows, stats = snapshot.normalize_rows(1, [raw], [], [], WAREHOUSE)
        self.assertIsNone(rows[0]['sizeList'][0]['price'])
        self.assertEqual(rows[0]['repImg'], '')
        self.assertEqual(stats['missingPriceSpecs'], 2)
        self.assertEqual(stats['missingImages'], 1)

    def test_invalid_spec_unknown_prices_and_stock_fail_instead_of_partial_snapshot(self):
        for change in ({'nmID': True}, {'sizes': []}, {'sizes': [{'chrtID': None}]}, {'vendorCode': ''}):
            raw = card()
            raw.update(change)
            with self.subTest(change=change), self.assertRaises(PlatformApiError):
                snapshot.normalize_rows(1, [raw], [], [], WAREHOUSE)
        raw = card()
        raw['sizes'][1]['chrtID'] = 11
        with self.assertRaises(PlatformApiError):
            snapshot.normalize_rows(1, [raw], [], [], WAREHOUSE)
        for p, st in (([price(999)], []), ([dict(price(), sizes=[{'sizeID': 999}])], []),
                      ([], [{'chrtId': 999, 'amount': 1}])):
            with self.assertRaises(PlatformApiError):
                snapshot.normalize_rows(1, [card()], p, st, WAREHOUSE)

    def test_only_unique_active_moscow_id_is_used(self):
        self.assertEqual(snapshot.resolve_moscow([
            {'id': 9, 'name': '成都仓库'}, dict(WAREHOUSE, warehouseId=777, officeId=888),
        ])['id'], 42)
        for values in ([], [{'id': 9, 'name': '成都仓库'}],
                       [{'name': '莫斯科仓库', 'warehouseId': 777}],
                       [dict(WAREHOUSE, isProcessing=True)], [WAREHOUSE, dict(WAREHOUSE, id=43)]):
            with self.assertRaises(PlatformApiError):
                snapshot.resolve_moscow(values)

    def test_collect_closes_session_on_failure_and_never_queries_other_stores(self):
        api = Mock()
        api.warehouses.return_value = [WAREHOUSE, {'id': 99, 'name': '成都仓库'}]
        api.cards.return_value = [card()]
        api.prices.return_value = [price()]
        api.stocks.return_value = []
        with patch.object(snapshot, 'WBOpenAPI', return_value=api) as factory:
            rows, metadata = snapshot.collect_shop(OpenAPIShop(1, '店1', 'secret'))
            self.assertEqual(len(rows), 1)
            factory.assert_called_once_with('secret', region='cn')
            api.stocks.assert_called_once_with(42, [11, 12])
            self.assertEqual(metadata['warehouse']['id'], 42)
            api.close.assert_called_once()
        api.reset_mock()
        api.warehouses.return_value = [{'id': 99, 'name': '成都仓库'}]
        with patch.object(snapshot, 'WBOpenAPI', return_value=api), self.assertRaises(PlatformApiError):
            snapshot.collect_shop(OpenAPIShop(1, '店1', 'secret'))
        api.cards.assert_not_called()
        api.stocks.assert_not_called()
        api.close.assert_called_once()

    def test_same_vc_in_two_shops_keeps_independent_keys_nm_and_warehouse_ids(self):
        clients = {}
        for sid in (1, 2):
            api = Mock()
            api.warehouses.return_value = [dict(WAREHOUSE, id=40 + sid)]
            api.cards.return_value = [card(nm=100 + sid, specs=(sid,))]
            api.prices.return_value = [dict(price(nm=100 + sid), sizes=[{'sizeID': sid, 'price': sid}])]
            api.stocks.return_value = [{'chrtId': sid, 'amount': sid - 1}]
            clients[f'key-{sid}'] = api
        with patch.object(snapshot, 'WBOpenAPI', side_effect=lambda key, **kwargs: clients[key]):
            first, _ = snapshot.collect_shop(OpenAPIShop(1, '甲', 'key-1'))
            second, _ = snapshot.collect_shop(OpenAPIShop(2, '乙', 'key-2'))
        self.assertEqual(first[0]['vendorCode'], second[0]['vendorCode'])
        self.assertEqual((first[0]['nmId'], second[0]['nmId']), (101, 102))
        self.assertEqual(first[0]['sizeList'][0]['stockList'], [{'warehouseId': 41, 'amount': 0}])
        self.assertEqual(second[0]['sizeList'][0]['stockList'], [{'warehouseId': 42, 'amount': 1}])
        clients['key-1'].stocks.assert_called_once_with(41, [1])
        clients['key-2'].stocks.assert_called_once_with(42, [2])


class TestOpenAPIFetchWorkflow(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        previous = paths.DATA_DIR
        self.addCleanup(paths.configure, previous)
        self.data = Path(folder.name)
        paths.configure(self.data)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.output = io.StringIO()
        stack.enter_context(redirect_stdout(self.output))
        stack.enter_context(redirect_stderr(self.output))
        stack.enter_context(patch.object(products.time, 'sleep'))
        self.accesses = [OpenAPIShop(i, f'店{i}', f'secret-{i}') for i in (1, 2, 3)]
        stack.enter_context(patch.object(products.bcs.client, 'fetch_open_api_shops', return_value=self.accesses))
        self.sync = stack.enter_context(patch.object(products.bcs, 'sync_shop'))
        self.sync_all = stack.enter_context(patch.object(products.bcs, 'sync_shops_parallel'))
        self.legacy = stack.enter_context(patch.object(products.bcs, 'fetch_shop_products'))
        self.row = snapshot.normalize_rows(1, [card()], [price()], [], WAREHOUSE)[0][0]

    def run_cli(self, *args):
        from sellerops.cli.main import main
        return main(['--data-dir', str(self.data), *args])

    def test_default_partial_failure_preserves_old_and_continues_without_secret_leak(self):
        from sellerops.core.files import atomic_dump_json
        failed = Path(paths.shop_json_path(2))
        atomic_dump_json(failed, {'shopId': 2, 'rows': [{'old': True}]})
        before = failed.read_bytes()
        def collect(shop):
            if shop.id == 2:
                raise RuntimeError('remote error secret-2')
            return ([dict(self.row, shopId=shop.id)] if shop.id == 1 else []), {'warnings': {}}
        with patch.object(products.snapshot_open_api, 'collect_shop', side_effect=collect) as read:
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--no-sync'), 1)
        self.assertCountEqual([call.args[0].id for call in read.call_args_list], [1, 2, 3])
        self.assertEqual(failed.read_bytes(), before)
        self.assertEqual(json.loads(Path(paths.shop_json_path(3)).read_text(encoding='utf8'))['rows'], [])
        status = json.loads(Path(paths.STATUS_JSON).read_text(encoding='utf8'))
        self.assertEqual([error['id'] for error in status['errors']], [2])
        self.assertNotIn('secret-', Path(paths.STATUS_JSON).read_text(encoding='utf8') + self.output.getvalue())
        self.sync.assert_not_called()
        self.sync_all.assert_not_called()
        self.legacy.assert_not_called()
        self.assertFalse(Path(paths.MAPPING_XLSX).exists())

    def test_default_and_explicit_parallel_bound_with_isolated_shop_sessions(self):
        accesses = [OpenAPIShop(i, f'店{i}', f'secret-{i}') for i in range(1, 7)]
        real_collect = snapshot.collect_shop
        for args, limit in (([], 4), (['--parallel', '2'], 2), (['--parallel', '1'], 1)):
            with self.subTest(args=args):
                active = peak = 0
                lock, first_batch = Lock(), Barrier(limit)
                clients = {}
                for shop in accesses:
                    api = Mock()
                    api.warehouses.return_value = [dict(WAREHOUSE, id=40 + shop.id)]
                    api.cards.return_value = [card(nm=100 + shop.id, specs=(shop.id,))]
                    api.prices.return_value = [dict(price(nm=100 + shop.id), sizes=[
                        {'sizeID': shop.id, 'price': shop.id}])]
                    api.stocks.return_value = [{'chrtId': shop.id, 'amount': shop.id - 1}]
                    clients[shop.api_key] = api

                def collect(shop):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        if shop.id <= limit:
                            first_batch.wait(timeout=5)
                        return real_collect(shop)
                    finally:
                        with lock:
                            active -= 1

                with patch.object(products.bcs.client, 'fetch_open_api_shops', return_value=accesses) as read_keys, \
                        patch.object(snapshot, 'WBOpenAPI', side_effect=lambda key, **kw: clients[key]), \
                        patch.object(products.snapshot_open_api, 'collect_shop', side_effect=collect):
                    self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', *args), 0)
                self.assertEqual(peak, limit)
                read_keys.assert_called_once_with()
                status = json.loads(Path(paths.STATUS_JSON).read_text(encoding='utf8'))
                self.assertEqual(status['fetched'], [{'id': i, 'total': 1} for i in range(1, 7)])
                self.assertEqual(status['errors'], [])
                for shop in accesses:
                    doc = json.loads(Path(paths.shop_json_path(shop.id)).read_text(encoding='utf8'))
                    self.assertEqual(doc['shopId'], shop.id)
                    self.assertEqual(doc['warehouse']['id'], 40 + shop.id)
                    row = doc['rows'][0]
                    self.assertEqual(row['nmId'], 100 + shop.id)
                    self.assertEqual(row['sizeList'][0]['chrtId'], shop.id)
                    self.assertEqual(row['sizeList'][0]['stockList'], [
                        {'warehouseId': 40 + shop.id, 'amount': shop.id - 1}])
                    api = clients[shop.api_key]
                    api.stocks.assert_called_once_with(40 + shop.id, [shop.id])
                    api.close.assert_called_once_with()
        self.sync.assert_not_called()
        self.sync_all.assert_not_called()
        self.legacy.assert_not_called()

    def test_parallel_failures_keep_old_snapshots_and_sanitize_all_errors(self):
        from sellerops.core.files import atomic_dump_json
        accesses = self.accesses + [OpenAPIShop(4, '店4', 'secret-4')]
        previous = {}
        for shop in accesses:
            path = Path(paths.shop_json_path(shop.id))
            atomic_dump_json(path, {'shopId': shop.id, 'rows': [{'old': shop.id}]})
            previous[shop.id] = path.read_bytes()
        first_batch = Barrier(4)
        failures = {1: AuthenticationError('secret-1'), 2: RateLimitError('secret-2'),
                    3: NetworkError('secret-3')}

        def collect(shop):
            first_batch.wait(timeout=5)
            if shop.id in failures:
                raise failures[shop.id]
            return [], {'warnings': {}}

        with patch.object(products.bcs.client, 'fetch_open_api_shops', return_value=accesses), \
                patch.object(snapshot, 'collect_shop', side_effect=collect):
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi'), 1)
        for sid in failures:
            self.assertEqual(Path(paths.shop_json_path(sid)).read_bytes(), previous[sid])
        self.assertEqual(json.loads(Path(paths.shop_json_path(4)).read_text(encoding='utf8'))['rows'], [])
        status_text = Path(paths.STATUS_JSON).read_text(encoding='utf8')
        status = json.loads(status_text)
        self.assertEqual([row['id'] for row in status['errors']], [1, 2, 3])
        self.assertEqual(status['fetched'], [{'id': 4, 'total': 0}])
        self.assertNotIn('secret-', status_text + self.output.getvalue())
        self.sync.assert_not_called()
        self.sync_all.assert_not_called()
        self.legacy.assert_not_called()

    def test_parallel_empty_shop_list_and_invalid_values_do_not_start_collection(self):
        with patch.object(products.bcs.client, 'fetch_open_api_shops', return_value=[]) as keys, \
                patch.object(snapshot, 'collect_shop') as collect:
            for value in ('0', '-1'):
                self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--parallel', value), 1)
                self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--shop-id', '1', '--parallel', value), 1)
            for value in (0, -1, True, 1.5):
                with self.assertRaises(ValueError):
                    products.fetch_all(source='wb-openapi', parallel=value)
            keys.assert_not_called()
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--parallel', '100'), 0)
            collect.assert_not_called()
        status = json.loads(Path(paths.STATUS_JSON).read_text(encoding='utf8'))
        self.assertEqual((status['shops'], status['fetched'], status['errors']), ([], [], []))

    def test_concurrent_local_save_failure_does_not_stop_other_shops(self):
        from sellerops.core.files import atomic_dump_json
        path = Path(paths.shop_json_path(1))
        atomic_dump_json(path, {'shopId': 1, 'rows': [{'old': True}]})
        before = path.read_bytes()

        def save(filename, *args, **kwargs):
            if str(filename) == str(path):
                raise OSError('fixture disk full secret-1')
            atomic_dump_json(filename, *args, **kwargs)

        with patch.object(snapshot, 'collect_shop', return_value=([], {})), \
                patch.object(products, 'atomic_dump_json', side_effect=save):
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--parallel', '2'), 1)
        self.assertEqual(path.read_bytes(), before)
        status = json.loads(Path(paths.STATUS_JSON).read_text(encoding='utf8'))
        self.assertEqual([row['id'] for row in status['errors']], [1])
        self.assertEqual([row['id'] for row in status['fetched']], [2, 3])
        self.assertNotIn('secret-', Path(paths.STATUS_JSON).read_text(encoding='utf8') + self.output.getvalue())

    def test_single_shop_selection_empty_and_compatible_snapshot_fields(self):
        with patch.object(products.snapshot_open_api, 'collect_shop', return_value=(
            [self.row], {'warnings': {'missingStockSpecs': 2}, 'warehouse': WAREHOUSE, 'region': 'cn'},
        )) as read:
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--shop-id', '1'), 0)
            self.assertEqual(read.call_args.args[0].api_key, 'secret-1')
        from sellerops.persistence.snapshots import ProductSnapshotRepository
        from sellerops.features.catalog.mapping import price_of
        from sellerops.features.listings.plans import _snapshot_specs
        doc = ProductSnapshotRepository.load_snapshot_document(1)
        self.assertEqual(doc['source'], 'wb-openapi')
        self.assertEqual((doc['total'], doc['fetchedCount']), (1, 1))
        row = next(iter(ProductSnapshotRepository.load_shop_rows(1).values()))
        self.assertEqual(price_of(row), 0)
        self.assertEqual(_snapshot_specs(row), ([11, 12], None))
        from sellerops.features.listings import plans
        with patch.object(plans.warehouse, 'resolve_warehouse', return_value=42):
            plan = plans.plan_stock([row['vendorCode']], [1], {}, 0)[0]
        self.assertEqual(len(plan['items']), 2)
        self.assertTrue(all(item['cur_amt'] is None and not item['orig_zero'] for item in plan['items']))
        self.assertTrue(all(item['amount'] == 0 for item in plan['items']))
        self.assertFalse(Path(paths.shop_json_path(2)).exists())
        self.sync.assert_not_called()
        self.legacy.assert_not_called()

    def test_missing_token_never_constructs_success_or_falls_back(self):
        with patch.object(products.bcs.client, 'fetch_open_api_shops', return_value=[OpenAPIShop(1, '店1', '')]):
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi'), 1)
        self.assertFalse(Path(paths.shop_json_path(1)).exists())
        self.legacy.assert_not_called()
        self.sync_all.assert_not_called()

    def test_invalid_explicit_shop_id_does_not_expand_to_all_shops(self):
        with patch.object(products.bcs.client, 'fetch_open_api_shops') as read:
            for sid in ('0', '-1'):
                self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--shop-id', sid), 1)
            read.assert_not_called()
        self.legacy.assert_not_called()

    def test_explicit_bcs_preserves_no_sync_behavior(self):
        self.legacy.return_value = [self.row]
        self.assertEqual(self.run_cli('fetch', '--source', 'bcs', '--shop-id', '1', '--no-sync'), 0)
        self.legacy.assert_called_once_with(1, filter_type='BASE')
        self.sync.assert_not_called()
        doc = json.loads(Path(paths.shop_json_path(1)).read_text(encoding='utf8'))
        self.assertEqual(doc['source'], 'bcs')

    def test_failed_local_save_preserves_previous_snapshot(self):
        from sellerops.core.files import atomic_dump_json
        path = Path(paths.shop_json_path(1))
        atomic_dump_json(path, {'shopId': 1, 'rows': []})
        before = path.read_bytes()
        with patch.object(products.snapshot_open_api, 'collect_shop', return_value=([self.row], {})), \
                patch.object(products, 'atomic_dump_json', side_effect=OSError('fixture disk full')):
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--shop-id', '1'), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_official_snapshot_merges_without_changing_historical_ownership(self):
        from openpyxl import Workbook, load_workbook
        from sellerops.core.files import atomic_dump_json
        from sellerops.features.catalog import mapping
        vc = self.row['vendorCode']
        book = Workbook()
        book.active.title = 'Sheet1'
        book.active.append(['卖家SKU', '产品中文名', '实际售价', 'vendorCode前缀码'])
        book.active.append(['SKU1', '测试商品', 10, 'TEST'])
        book.save(paths.PRICE_BOOK_XLSX)
        book.close()
        atomic_dump_json(paths.VC_KNOWN_JSON, {vc: {'cn': '测试商品', 'dp': 10},
                                              'old-vc': {'cn': '历史商品', 'dp': 20}})
        atomic_dump_json(paths.VC_OVERRIDE_JSON, {})
        atomic_dump_json(paths.VC_EXCLUDED_JSON, {})
        atomic_dump_json(paths.STATUS_JSON, {'shops': [{'id': 1, 'name': '店1'}], 'errors': []})
        originals = {path: Path(path).read_bytes() for path in (
            paths.VC_KNOWN_JSON, paths.VC_OVERRIDE_JSON, paths.VC_EXCLUDED_JSON)}
        with patch.object(products.snapshot_open_api, 'collect_shop', return_value=([self.row], {})):
            self.assertEqual(self.run_cli('fetch', '--source', 'wb-openapi', '--shop-id', '1'), 0)
        self.assertEqual({path: Path(path).read_bytes() for path in originals}, originals)
        with patch.object(mapping, '_shop_id_cache', 1):
            self.assertEqual(self.run_cli('merge'), 0)
        result = load_workbook(paths.MAPPING_XLSX, read_only=True, data_only=True)
        try:
            rows = list(result['映射总表'].iter_rows(values_only=True))
            headers = list(rows[0])
            self.assertEqual(rows[1][headers.index('vendorCode')], vc)
            self.assertEqual(rows[1][headers.index('产品中文名')], '测试商品')
        finally:
            result.close()
        self.assertIn('old-vc', json.loads(Path(paths.VC_KNOWN_JSON).read_text(encoding='utf8')))


if __name__ == '__main__':
    unittest.main()

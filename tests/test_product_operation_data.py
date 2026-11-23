"""商品操作字段最小依赖、定向补齐与完整跳过报告；只用合成数据。"""

from catalog_fixture import document, corrupt_snapshot, map_vcs, write_book
from sellerops.features.catalog import selection
from contextlib import ExitStack, redirect_stdout
import csv
import io
import json
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.core.files import atomic_dump_json
from sellerops.features.products import dimension, plans, prices, product_data, stocks
from sellerops.features.products.discount_products import targeted_discount_plan
from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.persistence.snapshots import ProductSnapshotRepository as Snapshots
from sellerops.settings import paths

VC = 'BCS-HSJF-101'


def card(nm=101, specs=(201, 202), vc=VC):
    return {'nmID': nm, 'vendorCode': vc, 'sizes': [{'chrtID': chrt} for chrt in specs]}


class TestProductOperationData(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(self.data)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.accesses = [OpenAPIShop(sid, f'店{sid}', f'secret-{sid}') for sid in (1, 2)]
        self.keys = self.stack.enter_context(patch.object(
            product_data.bcs, 'fetch_open_api_shops', return_value=self.accesses))
        self.clients = {}
        for sid in (1, 2):
            api = Mock()
            api.cards.return_value = [card(nm=100 + sid, specs=(sid * 100 + 1, sid * 100 + 2))]
            self.clients[f'secret-{sid}'] = api
        self.factory = self.stack.enter_context(patch.object(
            product_data, 'WBOpenAPI', side_effect=lambda key, **kw: self.clients[key]))
        self.stack.enter_context(patch.object(plans.warehouse, 'resolve_warehouse', side_effect=lambda sid: sid * 1000))

    def save(self, sid, **values):
        row = dict(shopId=sid, vendorCode=VC, nmId=100 + sid, sizeList=[], **values)
        Snapshots.save_shop_products(sid, [row])
        return document(sid)

    def test_sufficient_ids_do_not_require_current_stock_price_images_or_dimensions(self):
        row = {'nmId': 101, 'sizeList': [{'chrtId': 201}, {'chrtId': 202}], 'dimensionsLength': None}
        with product_data.ProductDataResolver() as resolver:
            self.assertIsNone(resolver.ensure(1, VC, row, specs=True).reason)
            self.assertIsNone(resolver.ensure(1, VC, {'nmId': 101}).reason)
            # 库存只需要chrtId及已确认的仓；不要求nmId或旧库存。
            self.assertIsNone(resolver.ensure(1, VC, {'sizeList': row['sizeList']}, nm=False, specs=True).reason)
        self.keys.assert_not_called()
        self.factory.assert_not_called()

    def test_missing_specs_are_repaired_per_shop_without_refresh_or_stock_queries(self):
        before = {sid: self.save(sid) for sid in (1, 2)}
        with product_data.ProductDataResolver() as resolver:
            stock = plans.plan_stock([VC], [1, 2], {}, 0, resolver=resolver)
            # 同一轮计划重复解析使用内存结果，不重复请求。
            again = resolver.ensure(1, VC, {'nmId': 101, 'sizeList': []}, nm=False, specs=True)
        self.assertTrue(again.repaired)
        self.assertEqual([[it['chrtId'] for it in p['items']] for p in stock], [[101, 102], [201, 202]])
        self.assertEqual([[it['warehouseId'] for it in p['items']] for p in stock], [[1000, 1000], [2000, 2000]])
        self.assertTrue(all(it['cur_amt'] is None for p in stock for it in p['items']))
        self.keys.assert_called_once_with()
        for sid in (1, 2):
            self.assertEqual(document(sid), before[sid])
            api = self.clients[f'secret-{sid}']
            api.cards.assert_called_once_with(vendor_code=VC, max_pages=20)
            api.prices.assert_not_called()
            api.stocks.assert_not_called()
            api.warehouses.assert_not_called()
            api.close.assert_called_once_with()

    def test_price_dimension_and_discount_only_ignore_unrelated_missing_fields(self):
        self.save(1)
        with product_data.ProductDataResolver() as resolver:
            price = plans.plan_price([VC], [1], {}, 100, None, None, resolver=resolver)
            discount = plans.plan_price([VC], [1], {}, None, 30, None, keep_price=True,
                                        resolver=resolver, allow_discount_only=True)
            dims = dimension.build_plans([VC], [1], {}, dims=(10, 20, 30, .5), resolver=resolver)
        self.assertEqual(price[0]['items'][0]['price'], 100)
        self.assertEqual(dims[0]['items'][0]['nmId'], 101)
        payload = prices._build_payload(discount[0]['items'][0])
        self.assertEqual(payload['discount'], 30)
        self.assertNotIn('price', payload)
        self.keys.assert_not_called()

    def test_nm_only_repair_does_not_require_remote_specs_or_images(self):
        self.clients['secret-1'].cards.return_value = [card(specs=())]
        with product_data.ProductDataResolver() as resolver:
            result = resolver.ensure(1, VC, {'nmId': None, 'sizeList': []})
        self.assertIsNone(result.reason)
        self.assertEqual(result.row['nmId'], 101)
        self.assertEqual(result.row['sizeList'], [])

    def test_ambiguous_mismatched_or_incomplete_remote_card_blocks_whole_product(self):
        bad = [[card(nm=999)], [card(vc=VC + 'x')], [card(), card()],
               [card(specs=())], [card(specs=(201, 201))], [card(specs=(201, None))]]
        for response in bad:
            with self.subTest(response=response):
                self.clients['secret-1'].cards.return_value = response
                with product_data.ProductDataResolver() as resolver:
                    result = resolver.ensure(1, VC, {'nmId': 101, 'sizeList': []}, specs=True)
                self.assertIsNotNone(result.reason)
                self.assertEqual(result.row['sizeList'], [])
        with product_data.ProductDataResolver() as resolver:
            self.assertIsNotNone(resolver.ensure(1, VC, {'shopId': 2, 'nmId': 101}).reason)

    def test_auth_rate_or_network_error_stops_shop_only_and_never_leaks_secrets(self):
        for error in (AuthenticationError('secret-1'), RateLimitError('secret-1'), NetworkError('secret-1')):
            with self.subTest(error=type(error).__name__):
                api = self.clients['secret-1']
                api.reset_mock()
                api.cards.side_effect = error
                with product_data.ProductDataResolver() as resolver:
                    first = resolver.ensure(1, VC, {'nmId': None})
                    second = resolver.ensure(1, VC + '2', {'nmId': None})
                    other = resolver.ensure(2, VC, {'nmId': None})
                self.assertIsNotNone(first.reason)
                self.assertIsNotNone(second.reason)
                self.assertIsNone(other.reason)
                self.assertEqual(api.cards.call_count, 1)
                self.assertNotIn('secret-', first.reason + second.reason + self.output.getvalue())

    def test_bcs_access_error_is_cached_for_the_plan(self):
        self.keys.side_effect = RuntimeError('secret-1')
        with product_data.ProductDataResolver() as resolver:
            first = resolver.ensure(1, VC, {'nmId': None})
            second = resolver.ensure(2, VC, {'nmId': None})
        self.keys.assert_called_once_with()
        self.factory.assert_not_called()
        self.assertNotIn('secret-', first.reason + second.reason)

    def test_failed_repairs_leave_healthy_products_executable_and_write_full_skip_report(self):
        broken = [{'vendorCode': f'BCS-HSJF-{i}', 'nmId': 1000 + i, 'sizeList': []} for i in range(40)]
        Snapshots.save_shop_products(1, [*broken, {'vendorCode': VC, 'nmId': 101, 'sizeList': [{'chrtId': 201}]}])
        self.clients['secret-1'].cards.return_value = []
        with product_data.ProductDataResolver() as resolver:
            plan = plans.plan_stock([VC, *(row['vendorCode'] for row in broken)], [1], {}, 0, resolver=resolver)[0]
        self.assertEqual([it['chrtId'] for it in plan['items']], [201])
        self.assertEqual(len(plan['blocked_items']), 40)
        reports = list(paths.daily_log_dir().glob('商品操作跳过_*.csv'))
        self.assertEqual(len(reports), 1)
        with reports[0].open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 40)
        self.assertEqual({r['vendorCode'] for r in rows}, {r['vendorCode'] for r in broken})
        self.assertTrue(all(row['原因'] for row in rows))
        self.assertNotIn('secret-', reports[0].read_text(encoding='utf-8-sig'))

    def test_dimension_partial_plan_failure_is_logged_and_does_not_post_blocked_item(self):
        self.save(1)
        self.save(2)
        with product_data.ProductDataResolver() as resolver:
            plan = dimension.build_plans([VC], [1, 2], {}, dims=(10, 20, 30, .5), resolver=resolver)
        plan[1]['items'] = []
        plan[1]['blocked_items'] = [{'vc': VC, 'reason': 'fixture missing required ID'}]
        with patch.object(dimension, '_post', return_value=(True, 'accepted')) as submit, patch.object(dimension.time, 'sleep'):
            ok, fail, rows = dimension.apply_plans(plan)
        self.assertEqual((ok, fail), (1, 1))
        submit.assert_called_once()
        self.assertEqual(rows[-1][-2:], ['跳过', 'fixture missing required ID'])

    def test_discount_without_threshold_uses_no_price_or_spec_and_unknown_is_not_zero(self):
        args = SimpleNamespace(threshold=None, below=-1, target=0, limit=0)
        with product_data.ProductDataResolver() as resolver:
            plan = targeted_discount_plan(1, [VC], {VC: {'nmId': 101, 'discount': None}}, {}, args, resolver)
        self.assertEqual(plan['blocked_items'], [])
        self.assertEqual(len(plan['items']), 1)
        self.assertIsNone(plan['items'][0]['old_discount'])
        self.keys.assert_not_called()

    def test_discount_threshold_requires_current_discount_even_with_a_cached_value(self):
        args = SimpleNamespace(threshold=50, below=-1, target=20, limit=0)
        api = self.clients['secret-1']
        api.prices.return_value = [{'nmID': 101, 'vendorCode': VC, 'discount': 60, 'sizes': []}]
        with product_data.ProductDataResolver() as resolver:
            plan = targeted_discount_plan(1, [VC], {VC: {'nmId': 101, 'discount': 40}}, {}, args, resolver)
        self.assertEqual(plan['blocked_items'], [])
        self.assertEqual(plan['items'][0]['old_discount'], 60)
        api.cards.assert_not_called()
        api.stocks.assert_not_called()
        api.prices.assert_called_once_with([101])

    def test_new_snapshot_vcs_with_prefix_remain_pending_until_confirmed(self):
        from sellerops.persistence import db

        self.save(1)
        write_book([('HSJF', 'fixture', 100, 'HSJF', '')])
        selector = SimpleNamespace(name='fixture')
        state, _, book = selection.load_state([1])
        self.assertEqual(selection.resolve_filters(selector, state, book)[0], [])
        self.assertEqual(db.resolve_vc(VC)['status'], 'pending')
        map_vcs({VC: 'HSJF'})
        state, _, book = selection.load_state([1])
        self.assertEqual(selection.resolve_filters(selector, state, book)[0], [VC])

    def test_stock_cli_finishes_all_preparation_before_writing_and_never_refreshes_snapshots(self):
        from operation_fixture import run_business_cli as main
        from sellerops.features.catalog import products, vc_mapping
        from sellerops.integrations.wildberries import stocks as stock_api
        before = {sid: self.save(sid) for sid in (1, 2)}
        events = []
        for sid in (1, 2):
            api = self.clients[f'secret-{sid}']
            response = api.cards.return_value
            api.cards.side_effect = lambda response=response, sid=sid, **kw: (events.append(f'read-{sid}') or response)
        cred = Mock(root_version='fixture')
        cred.wb_shop.side_effect = lambda sid: {'shopId': sid}

        def submit(session, wh, rows):
            events.append(f'write-{wh // 1000}')
            self.assertTrue(all(row['amount'] == 0 for row in rows))
            return {'error': False}

        with patch.object(stocks.credentials, 'get', return_value=cred), \
                patch.object(stocks.wb_api, 'make_session'), patch.object(stocks.time, 'sleep'), \
                patch.object(stock_api, 'post_stocks', side_effect=submit), \
                patch.object(products, 'fetch_all') as fetch, patch.object(vc_mapping, 'run_batch_set') as review:
            code = main(['--data-dir', str(self.data), 'product', 'stock', '--vc', VC,
                         '--shops', '1,2', '--amount', '0', '--apply', '--yes'])
        self.assertEqual(code, 0)
        self.assertEqual(events, ['read-1', 'read-2', 'write-1', 'write-2'])
        fetch.assert_not_called()
        review.assert_not_called()
        for sid in (1, 2):
            self.assertEqual(document(sid), before[sid])

    def test_snapshot_duplicate_vc_or_foreign_shop_is_not_silently_used(self):
        for doc in ({'shopId': 2, 'rows': [{'vendorCode': VC, 'nmId': 101}]},
                    {'shopId': 1, 'rows': [{'vendorCode': VC, 'nmId': 101}, {'vendorCode': VC, 'nmId': 999}]}):
            with self.subTest(doc=doc):
                self.save(1)
                from sellerops.persistence.snapshots import validate_document
                with self.assertRaises(ValueError):
                    validate_document(1, doc)
                corrupt_snapshot(1, snapshot_json=json.dumps({'shopId': 2}))
                self.assertIsNone(plans.load_shop_rows(1))
                result = plans.plan_stock([VC], [1], {}, 0)
                self.assertEqual(len(result[0]['blocked_items']), 1)
                self.assertEqual(result[0]['items'], [])

    def test_discount_cli_records_missing_id_and_submits_only_resolved_targets(self):
        from operation_fixture import run_business_cli as main
        from sellerops.features.products import discounts
        bad_vc = 'BCS-HSJF-bad'
        Snapshots.save_shop_products(1, [{'vendorCode': VC, 'nmId': 101},
                                       {'vendorCode': bad_vc, 'nmId': None}])
        self.clients['secret-1'].cards.return_value = []
        client = Mock()
        client.upload_batch_discount.return_value = Mock(success=True, task_id=123)
        cred = Mock(root_version='fixture')
        cred.wb_shop.return_value = {'shopId': 1, 'shopName': '店1'}
        with patch.object(discounts, 'WBClient', return_value=client), \
                patch.object(discounts.credentials, 'get', return_value=cred):
            self.assertEqual(main(['--data-dir', str(self.data), 'product', 'discount', '--shops', '1',
                                   '--vc', f'{VC},{bad_vc}', '--target', '0', '--apply']), 1)
        result = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(result['status'], 'partial')
        payload = client.upload_batch_discount.call_args.args[0]
        self.assertEqual(payload, [{'vendorCode': VC, 'nmID': 101, 'currencyIsoCode': 'CNY', 'discount': 0}])
        with Path(result['artifacts']['discount_csv']).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({row['status'] for row in rows}, {'blocked', 'accepted'})
        self.assertTrue(next(row['error'] for row in rows if row['status'] == 'blocked'))

    def test_live_stock_absence_keeps_all_confirmed_specs_and_reads_moscow_only(self):
        from sellerops.integrations.wildberries import stocks as stock_api
        Snapshots.save_shop_products(1, [{'vendorCode': VC, 'nmId': 101,
                                        'sizeList': [{'chrtId': 201}, {'chrtId': 202}]}])
        cred = Mock(root_version='fixture')
        cred.wb_shop.return_value = {'shopId': 1}
        session = Mock()
        with patch.object(stocks.credentials, 'get', return_value=cred), \
                patch.object(stocks.wb_api, 'make_session', return_value=session), \
                patch.object(stock_api, 'get_stocks', return_value=[
                    {'article': VC, 'chrtId': 201, 'amount': 0, 'storeId': 1000}]) as read:
            plan = stocks.build_plans([VC], [1], {}, 9, resolve='live')[0]
        self.assertEqual([item['chrtId'] for item in plan['items']], [201, 202])
        self.assertEqual([item['cur_amt'] for item in plan['items']], [0, None])
        self.assertEqual(plan['warehouses'][0]['stockItems'], [{'chrtId': 201, 'amount': 9}, {'chrtId': 202, 'amount': 9}])
        read.assert_called_once_with(session, store_id=1000, verbose=True, max_pages=200)
        session.close.assert_called_once_with()
        self.keys.assert_not_called()

    def test_live_stock_foreign_warehouse_blocks_instead_of_reusing_other_stock(self):
        from sellerops.integrations.wildberries import stocks as stock_api
        Snapshots.save_shop_products(1, [{'vendorCode': VC, 'nmId': 101, 'sizeList': [{'chrtId': 201}]}])
        cred = Mock(root_version='fixture')
        cred.wb_shop.return_value = {'shopId': 1}
        with patch.object(stocks.credentials, 'get', return_value=cred), \
                patch.object(stocks.wb_api, 'make_session'), \
                patch.object(stock_api, 'get_stocks', return_value=[
                    {'article': VC, 'chrtId': 201, 'amount': 99, 'storeId': 9999}]):
            plan = stocks.build_plans([VC], [1], {}, 0, resolve='live')[0]
        self.assertEqual(plan['items'], [])
        self.assertEqual(plan['warehouses'], [])
        self.assertEqual(len(plan['blocked_items']), 1)

    def test_bcs_keep_price_blocks_different_and_missing_spec_prices(self):
        self.save(1)
        api = self.clients['secret-1']
        api.prices.return_value = [{'nmID': 101, 'vendorCode': VC, 'sizes': [
            {'sizeID': 102, 'price': 20}, {'sizeID': 101, 'price': 30}]}]
        with product_data.ProductDataResolver() as resolver:
            result = plans.plan_price([VC], [1], {}, None, 10, None, keep_price=True, resolver=resolver)
        self.assertEqual(result[0]['items'], [])
        self.assertEqual(len(result[0]['blocked_items']), 1)
        api.prices.assert_called_once_with([101])
        api.prices.return_value = [{'nmID': 101, 'vendorCode': VC, 'sizes': [
            {'sizeID': 102, 'price': 20.25}, {'sizeID': 101, 'price': 20.25}]}]
        with product_data.ProductDataResolver() as resolver:
            result = plans.plan_price([VC], [1], {}, None, 10, None, keep_price=True, resolver=resolver)
        self.assertEqual(result[0]['items'][0]['price'], 20.25)
        api.prices.return_value = [{'nmID': 101, 'vendorCode': VC, 'sizes': [{'sizeID': 102, 'price': 20}]}]
        with product_data.ProductDataResolver() as resolver:
            result = plans.plan_price([VC], [1], {}, None, 10, None, keep_price=True, resolver=resolver)
        self.assertEqual(result[0]['items'], [])
        self.assertEqual(len(result[0]['blocked_items']), 1)

    def test_discount_corrupt_shop_is_reported_while_next_shop_can_be_submitted(self):
        from operation_fixture import run_business_cli as main
        from sellerops.features.products import discounts

        self.save(1)
        corrupt_snapshot(1, snapshot_json='{"shopId": 999}')
        self.save(2)
        client = Mock()
        client.upload_batch_discount.return_value = Mock(success=True, task_id=123)
        cred = Mock(root_version='fixture')
        cred.wb_shop.side_effect = lambda sid: {'shopId': sid}
        with patch.object(discounts, 'WBClient', return_value=client), \
                patch.object(discounts.credentials, 'get', return_value=cred):
            self.assertEqual(main(['--data-dir', str(self.data), 'product', 'discount', '--shops', '1,2',
                                   '--vc', VC, '--target', '20', '--apply']), 1)
        result = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(result['status'], 'partial')
        first, second = result['per_shop']
        self.assertIsNone(first['products']['matched'])
        self.assertFalse(first['range_complete'])
        self.assertEqual(second['task_ids'], ['123'])
        self.assertEqual(client.upload_batch_discount.call_args.args[0][0]['nmID'], 102)
        with Path(result['artifacts']['discount_csv']).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        failed = next(row for row in rows if row['status'] == 'blocked')
        self.assertEqual(failed['shop_id'], '1')
        self.assertIn('快照缺失或不可读', failed['error'])


if __name__ == '__main__':
    unittest.main()

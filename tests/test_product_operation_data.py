"""商品操作字段最小依赖、定向补齐与完整跳过报告；只用合成数据。"""
from contextlib import ExitStack, redirect_stdout
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.core.files import atomic_dump_json
from sellerops.domain.models import DiscountPlan
from sellerops.features.listings import dimension, plans, prices, product_data, stocks
from sellerops.features.pricing.discount_products import snapshot_discount_items
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
        self.addCleanup(paths.configure, paths.DATA_DIR)
        paths.configure(self.data)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.accesses = [OpenAPIShop(sid, f'店{sid}', f'secret-{sid}') for sid in (1, 2)]
        self.keys = self.stack.enter_context(patch.object(
            product_data.bcs.client, 'fetch_open_api_shops', return_value=self.accesses))
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
        return Path(paths.shop_json_path(sid)).read_bytes()

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
            self.assertEqual(Path(paths.shop_json_path(sid)).read_bytes(), before[sid])
            api = self.clients[f'secret-{sid}']
            api.cards.assert_called_once_with(vendor_code=VC, max_pages=20)
            api.prices.assert_not_called()
            api.stocks.assert_not_called()
            api.warehouses.assert_not_called()
            api.close.assert_called_once_with()

    def test_price_dimension_and_discount_only_ignore_unrelated_missing_fields(self):
        self.save(1)
        with product_data.ProductDataResolver() as resolver:
            price = plans.plan_price([VC], [1], {}, [], 100, None, None, resolver=resolver)
            discount = plans.plan_price([VC], [1], {}, [], None, 30, None, keep_price=True,
                                        resolver=resolver, allow_discount_only=True)
            dims = dimension.build_plans([VC], [1], {}, {}, dims=(10, 20, 30, .5), resolver=resolver)
        self.assertEqual(price[0]['items'][0]['price'], 100)
        self.assertEqual(dims[0]['items'][0]['nmId'], 101)
        payload = prices._build_payload(discount[0]['items'][0], 30)
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
        reports = list(Path(paths.LOG_DIR).glob('商品操作跳过_*.csv'))
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
            plan = dimension.build_plans([VC], [1, 2], {}, {}, dims=(10, 20, 30, .5), resolver=resolver)
        plan[1]['items'] = []
        plan[1]['blocked_items'] = [{'vc': VC, 'reason': 'fixture missing required ID'}]
        with patch.object(dimension, '_post', return_value=(True, 'accepted')) as submit, patch.object(dimension.time, 'sleep'):
            ok, fail, rows = dimension.apply_plans(plan)
        self.assertEqual((ok, fail), (1, 1))
        submit.assert_called_once()
        self.assertEqual(rows[-1][-2:], ['跳过', 'fixture missing required ID'])

    def test_discount_without_threshold_uses_no_price_or_spec_and_unknown_is_not_zero(self):
        from sellerops.features.pricing.discounts import Discounts
        plan = DiscountPlan(target_vcs=[VC], threshold=-1, target_discount=0)
        tracker = plans.SkipTracker()
        items, blocked = snapshot_discount_items(1, {VC: {'nmId': 101, 'discount': None}}, plan,
                                                 lambda *args: '黑丝假发', Discounts._matches_filters,
                                                 Discounts._disc_matched, tracker)
        self.assertEqual(blocked, [])
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]['old_discount'])
        self.keys.assert_not_called()

    def test_discount_threshold_requires_and_repairs_only_discount_field(self):
        from sellerops.features.pricing.discounts import Discounts
        plan = DiscountPlan(target_vcs=[VC], threshold=50, target_discount=20)
        api = self.clients['secret-1']
        api.prices.return_value = [{'nmID': 101, 'vendorCode': VC, 'discount': 60, 'sizes': []}]
        items, blocked = snapshot_discount_items(1, {VC: {'nmId': 101}}, plan,
                                                 lambda *args: '黑丝假发', Discounts._matches_filters,
                                                 Discounts._disc_matched, plans.SkipTracker())
        self.assertEqual(blocked, [])
        self.assertEqual(items[0]['old_discount'], 60)
        api.cards.assert_not_called()
        api.stocks.assert_not_called()
        api.prices.assert_called_once_with([101])

    def test_new_snapshot_vcs_are_selected_by_name_without_writing_mapping_or_overrides(self):
        from sellerops.persistence.mappings import MappingRepository
        self.save(1)
        atomic_dump_json(paths.VC_EXCLUDED_JSON, {})
        previous = Path(paths.VC_EXCLUDED_JSON).read_bytes()
        boss = [{'cn': '黑丝假发', 'sku': 'HSJF', 'prefix': 'HSJF', 'dp': 100, 'floor': 100}]
        with patch.object(MappingRepository, 'load_mapping_state', return_value=({}, {})), \
                patch.object(MappingRepository, 'load_price_book', return_value=boss), \
                patch.object(MappingRepository, 'build_vc_resolver', return_value=(lambda *args: '黑丝假发', {})):
            from types import SimpleNamespace
            vcs, _ = plans.resolve_filters(SimpleNamespace(name='黑丝假发'))
            self.assertEqual(vcs, [VC])
        self.assertFalse(Path(paths.MAPPING_XLSX).exists())
        self.assertEqual(Path(paths.VC_EXCLUDED_JSON).read_bytes(), previous)

    def test_stock_cli_finishes_all_preparation_before_writing_and_never_refreshes_snapshots(self):
        from sellerops.cli.main import main
        from sellerops.features.catalog import products, mapping_sync
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
                patch.object(stock_api, 'post_stocks', side_effect=submit), patch.object(stocks, '_append_csv'), \
                patch.object(products, 'fetch_all') as fetch, patch.object(mapping_sync, 'run_merge') as merge:
            code = main(['--data-dir', str(self.data), 'stock', '--vc', VC,
                         '--shops', '1,2', '--amount', '0', '--apply', '--yes'])
        self.assertEqual(code, 0)
        self.assertEqual(events, ['read-1', 'read-2', 'write-1', 'write-2'])
        fetch.assert_not_called()
        merge.assert_not_called()
        for sid in (1, 2):
            self.assertEqual(Path(paths.shop_json_path(sid)).read_bytes(), before[sid])

    def test_snapshot_duplicate_vc_or_foreign_shop_is_not_silently_used(self):
        for doc in ({'shopId': 2, 'rows': [{'vendorCode': VC, 'nmId': 101}]},
                    {'shopId': 1, 'rows': [{'vendorCode': VC, 'nmId': 101}, {'vendorCode': VC, 'nmId': 999}]}):
            with self.subTest(doc=doc):
                atomic_dump_json(paths.shop_json_path(1), doc)
                self.assertIsNone(plans.load_shop_rows(1))
                result = plans.plan_stock([VC], [1], {}, 0)
                self.assertEqual(len(result[0]['blocked_items']), 1)
                self.assertEqual(result[0]['items'], [])

    def test_discount_service_records_missing_id_skip_and_submits_only_resolved_targets(self):
        from sellerops.features.pricing.discounts import Discounts
        from sellerops.persistence.mappings import MappingRepository
        bad_vc = 'BCS-HSJF-bad'
        Snapshots.save_shop_products(1, [{'vendorCode': VC, 'nmId': 101, 'discount': None},
                                       {'vendorCode': bad_vc, 'nmId': None}])
        self.clients['secret-1'].cards.return_value = []
        client = Mock()
        client.upload_batch_discount.return_value = Mock(success=True, task_id=123,
                                                          price_modal=False, quarantine_modal=False)
        plan = DiscountPlan(target_vcs=[VC, bad_vc], threshold=-1, target_discount=0, is_apply=True)
        with patch('sellerops.features.pricing.discounts.WBClient', return_value=client), \
                patch.object(MappingRepository, 'build_vc_resolver', return_value=(lambda *args: '黑丝假发', {})):
            result = Discounts().execute_plan(plan, [{'shopId': 1, 'shopName': '店1'}])
        self.assertEqual((result['total_applied'], result['total_failed']), (1, 1))
        payload = client.upload_batch_discount.call_args.args[0]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['vendorCode'], VC)
        self.assertNotIn('price', payload[0])
        with Path(result['log_path']).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({row['status'] for row in rows}, {'skip', 'success'})
        self.assertTrue(next(row['error'] for row in rows if row['status'] == 'skip'))

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

    def test_bcs_keep_price_repairs_prices_by_spec_id_and_blocks_missing_first_price(self):
        self.save(1)
        api = self.clients['secret-1']
        api.prices.return_value = [{'nmID': 101, 'vendorCode': VC, 'sizes': [
            {'sizeID': 102, 'price': 20}, {'sizeID': 101, 'price': 30}]}]
        with product_data.ProductDataResolver() as resolver:
            result = plans.plan_price([VC], [1], {}, [], None, 10, None, keep_price=True, resolver=resolver)
        self.assertEqual(result[0]['items'][0]['price'], 30)
        api.prices.assert_called_once_with([101])
        api.prices.return_value = [{'nmID': 101, 'vendorCode': VC, 'sizes': [{'sizeID': 102, 'price': 20}]}]
        with product_data.ProductDataResolver() as resolver:
            result = plans.plan_price([VC], [1], {}, [], None, 10, None, keep_price=True, resolver=resolver)
        self.assertEqual(result[0]['items'], [])
        self.assertEqual(len(result[0]['blocked_items']), 1)

    def test_discount_corrupt_shop_is_reported_while_next_shop_can_be_submitted(self):
        from sellerops.features.pricing.discounts import Discounts
        from sellerops.persistence.mappings import MappingRepository
        atomic_dump_json(paths.shop_json_path(1), {'shopId': 999, 'rows': [{'vendorCode': VC}]})
        self.save(2)
        client = Mock()
        client.upload_batch_discount.return_value = Mock(success=True, task_id=123,
                                                          price_modal=False, quarantine_modal=False)
        plan = DiscountPlan(target_vcs=[VC], threshold=-1, target_discount=20, is_apply=True)
        with patch('sellerops.features.pricing.discounts.WBClient', return_value=client), \
                patch.object(MappingRepository, 'build_vc_resolver', return_value=(lambda *args: '黑丝假发', {})):
            result = Discounts().execute_plan(plan, [{'shopId': 1}, {'shopId': 2}])
        self.assertEqual((result['total_applied'], result['total_failed']), (1, 1))
        self.assertEqual(client.upload_batch_discount.call_args.args[0][0]['nmID'], 102)
        with Path(result['log_path']).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        failed = next(row for row in rows if row['status'] == 'skip')
        self.assertEqual(failed['shop_id'], '1')
        self.assertIn('准备失败', failed['error'])


if __name__ == '__main__':
    unittest.main()

"""Moscow-only planning and per-product stock-zero prerequisites, entirely offline."""
from contextlib import ExitStack, redirect_stdout
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


def item(nm, *specs):
    return {'vc': f'BCS-TEST-{nm}', 'cn': 'fixture', 'nmID': nm,
            'stock_specs': [(c, 100) for c in specs]}


def response(rows, failed=()):
    details = [{**r, 'ok': r['chrtId'] not in failed, 'msg': 'fixture'} for r in rows]
    ok = sum(r['ok'] for r in details)
    return {'ok': ok, 'fail': len(details) - ok, 'details': details}


class TestMoscowResolution(unittest.TestCase):
    def test_ambiguous_unavailable_and_invalid_moscow_are_not_cached(self):
        from sellerops.features.listings import warehouse
        cases = [
            [{'id': 100, 'name': '莫斯科一仓'}, {'id': 200, 'name': '莫斯科二仓'}],
            [{'id': 100, 'name': '莫斯科仓库', 'isProcessing': True}],
            [{'id': 100, 'name': '莫斯科仓库', 'isDeleting': True}],
            [{'warehouseId': 999, 'name': '莫斯科仓库'}],
            [{'id': 0, 'name': '莫斯科仓库'}],
            [{'id': True, 'name': '莫斯科仓库'}],
            [{'id': 1.5, 'name': '莫斯科仓库'}],
        ]
        self.addCleanup(warehouse.clear_cache)
        for stores in cases:
            with self.subTest(stores=stores), redirect_stdout(io.StringIO()):
                warehouse.clear_cache()
                with patch.object(warehouse, 'list_stores', side_effect=[stores, [{'id': 300, 'name': '莫斯科仓库'}]]) as read:
                    self.assertIsNone(warehouse.resolve_warehouse(42))
                    self.assertEqual(warehouse.resolve_warehouse(42), 300)
                    self.assertEqual(read.call_count, 2)

    def test_confirmed_ids_are_cached_separately_for_each_shop(self):
        from sellerops.features.listings import warehouse
        warehouse.clear_cache()
        self.addCleanup(warehouse.clear_cache)
        with patch.object(warehouse, 'list_stores', side_effect=lambda sid: [{'id': sid * 100, 'name': '莫斯科仓库', 'warehouseId': 9}]) as read:
            self.assertEqual(warehouse.resolve_warehouse(42), 4200)
            self.assertEqual(warehouse.resolve_warehouse(43), 4300)
            self.assertEqual(warehouse.resolve_warehouse(42), 4200)
        self.assertEqual(read.call_count, 2)


class TestLocalPlanning(unittest.TestCase):
    def setUp(self):
        from sellerops.settings import paths
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        previous = paths.DATA_DIR
        self.addCleanup(paths.configure, previous)
        paths.configure(self.data)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def save(self, rows):
        from sellerops.persistence.snapshots import ProductSnapshotRepository
        ProductSnapshotRepository.save_shop_products(42, rows)

    def run_cli(self, command, apply=False):
        from sellerops.cli.main import main
        return main(['--data-dir', str(self.data), command, '--vc', 'BCS-TEST-101',
                     *(['--apply', '--yes'] if apply else [])])

    def test_local_default_builds_complete_plan_without_remote_product_or_stock_reads(self):
        from sellerops.features.listings import warehouse, stocks, trash, plans
        from sellerops.integrations.wildberries import stocks as stock_api
        from sellerops.integrations.bcs import client as bcs
        self.save([{'vendorCode': 'BCS-TEST-101', 'nmId': 101,
                    'sizeList': [{'chrtId': 201, 'stockList': []}, {'chrtId': 202, 'stockList': []}]}])
        with patch.object(warehouse, 'resolve_warehouse', return_value=100), \
                patch.object(stock_api, 'get_stocks') as read_stock, \
                patch.object(bcs, 'fetch_shop_products') as read_products, \
                patch.object(stocks, 'set_stock') as write_stock, patch.object(trash, 'move_nms') as write_trash:
            for command in ['stock', 'trash']:
                self.assertEqual(self.run_cli(command), 0)
            result = plans.plan_trash(['BCS-TEST-101'], [42], {})
        self.assertEqual(result[0]['nmIds'], [101])
        self.assertEqual(result[0]['items'][0]['stock_specs'], [(201, 100), (202, 100)])
        for mock in [read_stock, read_products, write_stock, write_trash]:
            mock.assert_not_called()

    def test_missing_moscow_blocks_preview_and_apply_with_nonzero_exit(self):
        from sellerops.features.listings import warehouse, stocks, trash
        self.save([{'vendorCode': 'BCS-TEST-101', 'nmId': 101, 'sizeList': [{'chrtId': 201}]}])
        with patch.object(warehouse, 'resolve_warehouse', return_value=None), \
                patch.object(stocks.credentials, 'get', return_value=Mock(root_version='fixture')), \
                patch.object(stocks, 'set_stock') as clear, patch.object(trash, 'move_nms') as move:
            for command in ['stock', 'trash']:
                for apply in [False, True]:
                    self.assertEqual(self.run_cli(command, apply), 1)
        clear.assert_not_called()
        move.assert_not_called()

    def test_empty_or_partly_missing_specs_block_whole_product(self):
        from sellerops.features.listings import plans, warehouse
        for sizes in [[], [{'chrtId': 201}, {}], [{'chrtId': 201}, {'chrtId': 0}], [{'chrtId': True}]]:
            self.save([{'vendorCode': 'BCS-TEST-101', 'nmId': 101, 'sizeList': sizes}])
            with self.subTest(sizes=sizes), patch.object(warehouse, 'resolve_warehouse', return_value=100):
                for command in ['stock', 'trash']:
                    self.assertEqual(self.run_cli(command), 1)
                for planner, args in [(plans.plan_stock, (0,)), (plans.plan_trash, ())]:
                    result = planner(['BCS-TEST-101'], [42], {}, *args)
                    self.assertEqual(result[0]['items'], [])
                    self.assertEqual(len(result[0]['blocked_items']), 1)

    def test_missing_or_invalid_nm_id_cannot_be_moved(self):
        from sellerops.features.listings import warehouse
        for nm in [None, 0, True, 'invalid']:
            self.save([{'vendorCode': 'BCS-TEST-101', 'nmId': nm, 'sizeList': [{'chrtId': 201}]}])
            with self.subTest(nm=nm), patch.object(warehouse, 'resolve_warehouse', return_value=100):
                self.assertEqual(self.run_cli('trash'), 1)

    def test_empty_shop_does_not_request_warehouse_data(self):
        from sellerops.features.listings import plans, warehouse
        self.save([])
        with patch.object(warehouse, 'resolve_warehouse') as resolve:
            self.assertEqual(plans.plan_trash(['BCS-TEST-101'], [42], {}), [])
            self.assertEqual(plans.plan_stock(['BCS-TEST-101'], [42], {}, 0), [])
        resolve.assert_not_called()

    def test_cli_partial_plan_failure_still_executes_only_healthy_local_card(self):
        from sellerops.cli.main import main
        from sellerops.features.listings import warehouse, stocks, trash
        from sellerops.integrations.wildberries import stocks as stock_api
        self.save([
            {'vendorCode': 'BCS-TEST-101', 'nmId': 101, 'sizeList': [{'chrtId': 201}]},
            {'vendorCode': 'BCS-TEST-102', 'nmId': 102, 'sizeList': [{'chrtId': 202}, {}]},
        ])
        cred = Mock(root_version='fixture')
        cred.wb_shop.return_value = {'shopId': 42}
        with patch.object(warehouse, 'resolve_warehouse', return_value=100), \
                patch.object(stocks.credentials, 'get', return_value=cred), \
                patch.object(stocks, 'set_stock', side_effect=lambda shop, wh, rows, **kw: response(rows)) as clear, \
                patch.object(trash, 'move_nms', return_value={'ok': 1, 'fail': 0, 'details': [{'nmID': 101, 'ok': True, 'msg': 'OK'}]}) as move, \
                patch.object(stock_api, 'get_stocks') as read_stock:
            code = main(['--data-dir', str(self.data), 'trash', '--vc', 'BCS-TEST-101,BCS-TEST-102', '--apply', '--yes'])
        self.assertEqual(code, 1)
        self.assertEqual(clear.call_args.args[2], [{'chrtId': 201, 'amount': 0}])
        self.assertEqual(move.call_args.args[1], [101])
        read_stock.assert_not_called()


class TestTrashExecution(unittest.TestCase):
    def setUp(self):
        from sellerops.features.listings import execution, stocks, trash
        self.execution, self.stocks, self.trash = execution, stocks, trash
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.csv = Path(directory.name) / 'ops.csv'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(execution, 'RESULT_CSV', str(self.csv)))
        self.stack.enter_context(patch.object(execution.time, 'sleep'))
        self.cred = Mock(root_version='fixture')
        self.cred.wb_shop.side_effect = lambda sid: {'shopId': sid}
        self.stack.enter_context(patch.object(execution.credentials, 'get', return_value=self.cred))
        self.move = self.stack.enter_context(patch.object(trash, 'move_nms', side_effect=self.move_response))
        self.bcs = self.stack.enter_context(patch.object(execution, '_post', return_value=(True, 'OK')))

    def move_response(self, shop, ids, **kwargs):
        return {'ok': len(ids), 'fail': 0, 'details': [{'nmID': n, 'ok': True, 'msg': 'OK'} for n in ids]}

    def execute(self, items, channel='wb', blocked=None):
        return self.execution.run_apply([{'shopId': 42, 'items': items,
            'nmIds': [i.get('nmID') for i in items], 'blocked_items': blocked or []}], 'trash', channel=channel)

    def test_partial_clear_failure_blocks_entire_affected_card_only(self):
        events = []
        def clear(shop, wh, rows, **kwargs):
            events.append('clear')
            return response(rows, failed={201, 202})
        self.move.side_effect = lambda shop, ids, **kw: (events.append('trash') or self.move_response(shop, ids))
        with patch.object(self.stocks, 'set_stock', side_effect=clear) as submit:
            self.assertEqual(self.execute([item(101, 201, 202), item(102, 203)]), (1, 1))
        self.assertEqual(events, ['clear', 'trash'])
        self.assertEqual(submit.call_args.args[1], 100)
        self.assertTrue(all(r['amount'] == 0 for r in submit.call_args.args[2]))
        self.assertEqual(self.move.call_args.args[1], [102])
        self.bcs.assert_not_called()
        with self.csv.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(any(r['ID(nmId/chrtId)'] == '101' and r['接口响应'].startswith('SKIP:')
                            and 'fixture' in r['接口响应'] for r in rows))

    def test_failed_clearing_blocks_both_wb_and_bcs_trash_channels(self):
        for channel in ['wb', 'bcs']:
            with self.subTest(channel=channel), patch.object(self.stocks, 'set_stock', side_effect=lambda shop, wh, rows, **kw: response(rows, failed={201})):
                self.assertEqual(self.execute([item(101, 201)], channel), (0, 1))
        self.move.assert_not_called()
        self.bcs.assert_not_called()

    def test_incomplete_or_ambiguous_clear_receipts_do_not_allow_trash(self):
        cases = [
            {'ok': 2, 'fail': 0, 'details': [{'chrtId': 201, 'amount': 0, 'ok': True}]},
            {'ok': 2, 'fail': 0, 'details': [{'chrtId': 201, 'amount': 0, 'ok': True}] * 2},
            {'ok': 2, 'fail': 0, 'details': [{'chrtId': 999, 'amount': 0, 'ok': True}, {'chrtId': 202, 'amount': 0, 'ok': True}]},
        ]
        for result in cases:
            with self.subTest(result=result), patch.object(self.stocks, 'set_stock', return_value=result):
                self.assertEqual(self.execute([item(101, 201, 202)]), (0, 1))
        self.move.assert_not_called()

    def test_executor_also_rejects_direct_plans_without_complete_specs(self):
        with patch.object(self.stocks, 'set_stock') as clear:
            for bad in [item(101), item(None, 201), {**item(101, 201), 'stock_specs': [(201, None)]}]:
                self.assertEqual(self.execute([bad]), (0, 1))
        clear.assert_not_called()
        self.move.assert_not_called()

    def test_planning_failures_are_counted_alongside_healthy_cards(self):
        blocked = [{'vc': 'BCS-TEST-999', 'nmID': 999, 'reason': 'missing spec'}]
        with patch.object(self.stocks, 'set_stock', side_effect=lambda shop, wh, rows, **kw: response(rows)):
            self.assertEqual(self.execute([item(101, 201)], blocked=blocked), (1, 1))
        self.assertEqual(self.move.call_args.args[1], [101])

    def test_auth_failure_stops_shop_and_does_not_block_other_shops(self):
        def clear(shop, wh, rows, **kwargs):
            if shop['shopId'] == 42:
                raise self.stocks.common.CookieExpiredError('fixture expired')
            return response(rows)
        plans = [{'shopId': 42, 'items': [item(101, 201)], 'nmIds': [101]},
                 {'shopId': 43, 'items': [item(102, 202)], 'nmIds': [102]}]
        with patch.object(self.stocks, 'set_stock', side_effect=clear):
            self.assertEqual(self.execution.run_apply(plans, 'trash', channel='wb'), (1, 1))
        self.move.assert_called_once()
        self.assertEqual(self.move.call_args.args[0]['shopId'], 43)

    def test_successful_batches_are_not_replayed_after_later_stock_failure(self):
        def clear(shop, wh, rows, **kwargs):
            return response(rows, failed={202})
        with patch.object(self.stocks, 'STOCK_CHUNK', 1), patch.object(self.stocks, 'set_stock', side_effect=clear) as submit:
            self.assertEqual(self.execute([item(101, 201, 202), item(102, 203)]), (1, 1))
        self.assertEqual([c.args[2][0]['chrtId'] for c in submit.call_args_list], [201, 202, 203])
        self.assertEqual(self.move.call_args.args[1], [102])

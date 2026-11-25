"""Offline business regression cases with synthetic fixtures."""
import os
import time
import unittest


class TestRegression(unittest.TestCase):
    @staticmethod
    def _fb(found=None, stats=None, missing=None, pages=1):
            """构造 find_batches 返回值（离线单测用）。"""
            found = found or {}
            return {"found": found, "missing": set(missing or []),
                    "all_found": not (missing or []), "pages_fetched": pages,
                    "stats": stats or {}}

    def test_mabang_mapping_header_based(self):
        import tempfile
        from catalog_fixture import map_vcs, write_book
        from sellerops.settings import paths
        from sellerops.features.orders import matching

        self.addCleanup(paths.configure, paths.current.data_dir)
        with tempfile.TemporaryDirectory() as tmp:
            paths.configure(tmp)
            write_book([('TESTSKU-6161', 'S-1', 10, '测试商品', ' 马帮发货 ')],
                       ('马帮库存SKU', '卖家SKU', '最低售价', '产品中文名', '发货方式'))
            map_vcs({'BCS-AAAA-123': 'S-1'})
            names, stock, modes = matching.load_local_mapping()
            self.assertEqual(names, {'BCS-AAAA-123': '测试商品'})
            self.assertEqual(stock, {'BCS-AAAA-123': 'TESTSKU-6161'})
            self.assertEqual(modes, {'BCS-AAAA-123': '马帮发货'})

    def test_mabang_classifies_all_shipping_modes(self):
        from sellerops.features.orders import matching

        vc = 'BCS-AAAA-1'
        order = {'id': 1, 'shopIdText': '9352', 'order_ellipsis_other_title': f'数量:{vc}*1'}
        invalid_note = '价格表发货方式未填或无效，请填 马帮发货/WB发货/不处理'
        cases = [
            ('马帮发货', 'SKU-1', 'REPLACE', 'SKU-1', '系统匹配=(空)；字符串不一致'),
            ('马帮发货', '', 'NO_SKU', '', '发货方式为马帮发货，但缺少马帮库存SKU'),
            ('WB发货', 'SKU-1', 'GROUP_SHIPPING', '', '价格表标记WB发货，跳过马帮匹配、预报和交运'),
            ('不处理', 'SKU-1', 'SKIP_SHIPPING', '', '价格表标记不处理，跳过发货'),
            ('', 'SKU-1', 'NO_SKU', '', invalid_note),
            ('无效', 'SKU-1', 'NO_SKU', '', invalid_note),
        ]
        for mode, sku, status, expected_sku, note in cases:
            with self.subTest(mode=mode, sku=sku):
                [row] = matching.classify([order], {vc: '测试商品'}, {vc: sku}, {vc: mode},
                                         {'9352': '袁州1(9352)'})
                self.assertEqual((row['status'], row['expected_sku'], row['note']),
                                 (status, expected_sku, note))

    def test_mabang_process_upload_wait_unit(self):
        """离线单测：正向判据 —— 命中 status=3「预报成功」即完成 + 店铺映射提取。

            预扫描 status=1 命中（上传中 status=2，建立映射）；
            第 1 轮 status=3 未命中、第 2 轮 status=3 命中 ⇒ done=True。
            并校验新节奏：status=1 仅预扫描一次（不再每轮查）。
            """
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang
        row = {'batchNo': 'B1', 'shopId': '子龙主2', 'status': 3, 'total': 3, 'successNum': 3, 'failNum': 0, 'handoverNumber': 'WB-GI-1'}
        up1 = {'batchNo': 'B1', 'shopId': '子龙主2', 'status': 2, 'total': 3, 'successNum': 0, 'failNum': 0}
        rounds, calls = ({'n': 0}, [])

        def fake_find(cred, batch_nos, status, rows_per_page=200, max_pages=50):
            calls.append(status)
            if status == 3:
                rounds['n'] += 1
                if rounds['n'] >= 2:
                    return self._fb(found={'B1': row}, stats={'succesTotal': 1})
                return self._fb(stats={'succesTotal': 0, 'waitTotal': 1})
            if status == 1:
                return self._fb(found={'B1': up1}, stats={'waitTotal': 1})
            return self._fb()
        with mock.patch.object(mabang, 'find_batches', side_effect=fake_find), mock.patch.object(mabang.time, 'sleep', lambda *_: None):
            res = mabang.wait_upload_done(None, ['B1'], timeout=60, interval=1)
        self.assertTrue(res['done'], '命中 status=3 后应判定上传成功完成')
        self.assertEqual(res['success'], {'B1'})
        self.assertEqual(res['store_of'].get('B1'), '子龙主2', '应记录 batchNo→shopId 映射')
        self.assertTrue(res['rows'].get('B1'), '应保留批次快照')
        self.assertEqual(res['failed'], set())
        self.assertEqual(calls.count(1), 1, 'status=1 应只在预扫描查一次（每轮仅查 status=3）')
        self.assertGreaterEqual(calls.count(3), 1, '主循环应轮询 status=3')

    def test_mabang_process_upload_wait_timeout_unit(self):
        """离线单测：轮询超时且批次仍在待预报 ⇒ done=False，返回未完成集合与映射。"""
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang
        row = {'batchNo': 'B9', 'shopId': '子龙主2（2）', 'status': 1, 'total': 2, 'successNum': 0, 'failNum': 0}
        clock = {'v': 0}

        def fake_find(cred, batch_nos, status, rows_per_page=200, max_pages=50):
            return self._fb(found={'B9': row}, stats={'waitTotal': 1}) if status == 1 else self._fb()

        def fake_time():
            clock['v'] += 10
            return clock['v']
        with mock.patch.object(mabang, 'find_batches', side_effect=fake_find), mock.patch.object(mabang.time, 'sleep', lambda *_: None), mock.patch.object(mabang.time, 'time', fake_time):
            res = mabang.wait_upload_done(None, ['B9'], timeout=5, interval=1)
        self.assertFalse(res['done'], '批次始终未成功时应判定超时')
        self.assertEqual(res['still'], {'B9'})
        self.assertEqual(res['failed'], set())
        self.assertEqual(res['store_of'].get('B9'), '子龙主2（2）')

    def test_mabang_process_forecast_failed_unit(self):
        """离线单测：超时后经 status=99 定性为「预报失败」⇒ failed 集合（不交运）。"""
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang
        fr = {'batchNo': 'B7', 'shopId': '子龙主2', 'status': 99, 'total': 2, 'successNum': 0, 'failNum': 2}
        clock = {'v': 0}

        def fake_find(cred, batch_nos, status, rows_per_page=200, max_pages=50):
            return self._fb(found={'B7': fr}, stats={'failTotal': 1}) if status == 99 else self._fb()

        def fake_time():
            clock['v'] += 10
            return clock['v']
        with mock.patch.object(mabang, 'find_batches', side_effect=fake_find), mock.patch.object(mabang.time, 'sleep', lambda *_: None), mock.patch.object(mabang.time, 'time', fake_time):
            res = mabang.wait_upload_done(None, ['B7'], timeout=5, interval=1)
        self.assertFalse(res['done'])
        self.assertEqual(res['failed'], {'B7'})
        self.assertEqual(res['still'], set(), '定性命中失败的批次不应再计入未完成')
        self.assertEqual(res['store_of'].get('B7'), '子龙主2')

    def test_mabang_process_history_success_unit(self):
        """离线单测：批次已归档到 status=5「历史预报」亦算成功（防永不命中而空等超时）。

            同时校验 status=5 兜底的**限频**：连续 3 轮 status=3 未命中才触发一次。
            """
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang
        row = {'batchNo': 'B1', 'shopId': '袁州3', 'status': 3, 'isHistoryStatus': '5', 'total': 1, 'successNum': 1, 'failNum': 0}
        calls = []

        def fake_find(cred, batch_nos, status, rows_per_page=200, max_pages=50):
            calls.append(status)
            return self._fb(found={'B1': row}, stats={'historyTotal': 1}) if status == 5 else self._fb()
        with mock.patch.object(mabang, 'find_batches', side_effect=fake_find), mock.patch.object(mabang.time, 'sleep', lambda *_: None):
            res = mabang.wait_upload_done(None, ['B1'], timeout=60, interval=1)
        self.assertTrue(res['done'])
        self.assertEqual(res['success'], {'B1'})
        self.assertEqual(res['store_of'].get('B1'), '袁州3')
        self.assertEqual(calls.count(5), 1, 'status=5 兜底应限频（3 轮未命中触发一次）')
        self.assertEqual(calls.count(1), 1, 'status=1 仅预扫描一次')

    def test_mabang_process_prescan_map_timeout_unit(self):
        """离线单测：预扫描建立店铺映射；status=3 恒不命中 ⇒ 超时时映射仍齐全（避免整体跳过）。"""
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang
        row = {'batchNo': 'B1', 'shopId': '子龙主2', 'status': 2, 'total': 3, 'successNum': 0, 'failNum': 0}
        calls, clock = ([], {'v': 0})

        def fake_find(cred, batch_nos, status, rows_per_page=200, max_pages=50):
            calls.append(status)
            return self._fb(found={'B1': row}, stats={'waitTotal': 1}) if status == 1 else self._fb()

        def fake_time():
            clock['v'] += 10
            return clock['v']
        with mock.patch.object(mabang, 'find_batches', side_effect=fake_find), mock.patch.object(mabang.time, 'sleep', lambda *_: None), mock.patch.object(mabang.time, 'time', fake_time):
            res = mabang.wait_upload_done(None, ['B1'], timeout=5, interval=1)
        self.assertFalse(res['done'])
        self.assertEqual(res['still'], {'B1'})
        self.assertEqual(res['store_of'].get('B1'), '子龙主2', '预扫描应建立店铺映射')
        self.assertEqual(calls.count(1), 1, '映射已齐全 ⇒ 超时补查 status=1 应跳过')

    def test_mabang_process_fallback5_throttled_unit(self):
        """离线单测：status=3 连续未命中时 status=5 兜底触发，且全程只触发一次（限频）。"""
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang
        calls = []
        fake_time = type('T', (), {'t': 0})()

        def _time():
            fake_time.t += 1
            return fake_time.t

        def fake_find(cred, batch_nos, status, rows_per_page=200, max_pages=50):
            calls.append(status)
            return self._fb(stats={'waitTotal': 1})
        with mock.patch.object(mabang, 'find_batches', side_effect=fake_find), mock.patch.object(mabang.time, 'sleep', lambda *_: None), mock.patch.object(mabang.time, 'time', _time):
            res = mabang.wait_upload_done(None, ['B1'], timeout=8, interval=1)
        self.assertFalse(res['done'])
        self.assertEqual(calls.count(5), 1, 'status=5 兜底应限频为一次')

    def test_mabang_process_fail_early_unit(self):
        """离线单测：failTotal 上涨 ⇒ 立刻核实 status=99 定性失败并早退，不空等到超时。

            复现 2026-10-17 袁州1 29 单场景：批次落在 status=99，但轮询只看 status=3，
            旧逻辑要等满 900s 才定性（白等 40+ 轮）；修复后应在 failTotal 一涨就收口。
            """
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang
        fr = {'batchNo': 'B1', 'shopId': '袁州1', 'status': 99, 'total': 29, 'successNum': 0, 'failNum': 29}
        state = {'r3': 0, 'fail': 0}
        calls = []

        def fake_find(cred, batch_nos, status, rows_per_page=200, max_pages=50):
            calls.append(status)
            if status == 3:
                state['r3'] += 1
                if state['r3'] >= 2:
                    state['fail'] = 1
                return self._fb(stats={'failTotal': state['fail'], 'waitTotal': 0})
            if status == 99:
                return self._fb(found={'B1': fr}, stats={'failTotal': state['fail']})
            return self._fb(stats={'failTotal': 0})
        clock = {'v': 0}

        def fake_time():
            clock['v'] += 20
            return clock['v']
        with mock.patch.object(mabang, 'find_batches', side_effect=fake_find), mock.patch.object(mabang.time, 'sleep', lambda *_: None), mock.patch.object(mabang.time, 'time', fake_time):
            res = mabang.wait_upload_done(None, ['B1'], timeout=900, interval=20)
        self.assertFalse(res['done'])
        self.assertEqual(res['failed'], {'B1'})
        self.assertEqual(res['still'], set(), '已定性失败的批次不算未完成')
        self.assertEqual(res['reason'], 'failed')
        self.assertEqual(calls.count(99), 1, '应对 status=99 核实一次')
        self.assertLessEqual(calls.count(3), 3, 'failTotal 上涨后应早退，不应跑满 900s/45 轮')

    def test_mabang_process_find_batches_pagination_unit(self):
        """离线单测：find_batches 首批未命中时按 pageHtml 页数自动翻页并命中。"""
        from unittest import mock
        from sellerops.features.orders import forecast_polling as mabang

        def side(cred, status=1, rows_per_page=200, page=1, **kw):
            if status != 3:
                return ([], {})
            if page == 1:
                return ([{'batchNo': f'X{i}'} for i in range(200)], {'pageTotal': 201, 'pageCount': 2, 'pageCur': 1})
            return ([{'batchNo': 'ZZ', 'shopId': '主号7', 'status': 3}], {'pageTotal': 201, 'pageCount': 2, 'pageCur': 2})
        with mock.patch.object(mabang, 'get_forecast_list', side_effect=side):
            res = mabang.find_batches(None, ['ZZ'], status=3, rows_per_page=200)
        self.assertTrue(res['all_found'], '翻页后应命中后段批次')
        self.assertEqual(res['pages_fetched'], 2)
        self.assertEqual(res['found']['ZZ']['shopId'], '主号7')

    def test_mabang_group_shipping_stays_excluded_if_price_book_changes_mid_run(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from sellerops.features.orders import forecast_selection, matching
        from sellerops.integrations.wildberries import client as wb
        from sellerops.persistence.price_book import SHIPPING_MODE_MABANG, SHIPPING_MODE_SKIP, SHIPPING_MODE_WB

        cred = {'shop_map': {'9352': '袁州1(9352)'}}
        marked = {'id': 1, 'shopIdText': '9352', 'platformOrderId': '101',
                  'order_ellipsis_other_title': '商品编号*数量:BCS-AAAA-1*1'}
        normal = dict(marked, id=2, platformOrderId='102',
                      order_ellipsis_other_title='商品编号*数量:BCS-BBBB-2*1')
        skipped = dict(marked, id=3, platformOrderId='103',
                       order_ellipsis_other_title='商品编号*数量:BCS-CCCC-3*1')
        names = {'BCS-AAAA-1': '群发商品', 'BCS-BBBB-2': '普通商品', 'BCS-CCCC-3': '跳过商品'}
        sku_values = {'BCS-BBBB-2': 'SKU-2'}
        modes = {'BCS-AAAA-1': SHIPPING_MODE_WB, 'BCS-BBBB-2': SHIPPING_MODE_MABANG,
                 'BCS-CCCC-3': SHIPPING_MODE_SKIP}
        args = SimpleNamespace(days=0, page_size=100, apply=True)

        with patch.object(matching, 'get_mabang_cred', return_value=cred), \
                patch.object(matching, 'fetch_pending_orders', return_value=[marked, skipped]), \
                patch.object(matching, 'load_local_mapping', return_value=(names, sku_values, modes)), \
                patch.object(matching, 'write_csv') as report, \
                patch.object(matching, 'api_ready') as api_ready, \
                patch.object(matching, 'search_stock') as search:
            matched = matching.match_orders(days=args.days, page_size=args.page_size, apply=args.apply)
            self.assertFalse(matched.stopped)
        api_ready.assert_not_called()
        search.assert_not_called()
        self.assertEqual(matched.group_ids, {'1'})
        self.assertEqual(matched.failures, {})
        self.assertEqual([row['status'] for row in report.call_args.args[0]],
                         [matching.STATUS_GROUP_SHIPPING, matching.STATUS_SKIP_SHIPPING])

        with patch.object(forecast_selection, 'get_mabang_cred', return_value=cred), \
                patch.object(forecast_selection, 'fetch_pending_orders', return_value=[marked, normal, skipped]), \
                patch.object(forecast_selection, 'load_local_mapping',
                             side_effect=[(names, sku_values, modes),
                                          (names, {**sku_values, 'BCS-AAAA-1': 'SKU-1'},
                                           {**modes, 'BCS-AAAA-1': SHIPPING_MODE_MABANG})]), \
                patch.object(wb, 'fetch_canceled_ids', return_value=set()):
            direct_args = SimpleNamespace(days=0, page_size=100)
            for forecast_args in (direct_args, args):
                target, _ = forecast_selection.collect_forecast_orders(forecast_args, group_shipping_ids=matched.group_ids if forecast_args is args else ())
                self.assertEqual([r['order_id'] for r in target], [2])

    def test_mabang_stock_daily_counts_empty_sku_separately(self):
        from unittest.mock import patch
        from sellerops.features.orders.mabang_stock_daily import read_orders_daily

        day = '2026-11-11'
        rows = [{'fields': {'日期': day, '库存SKU': sku, '订单量': qty}}
                for sku, qty in [(None, 2), ('SKU-2', 3)]]
        with patch('sellerops.integrations.feishu.tables.list_records', return_value=rows):
            daily, no_sku = read_orders_daily('fixture', 'orders', day, day)
        self.assertEqual(daily, {day: {'SKU-2': 3}})
        self.assertEqual(no_sku, {day: 2})

    def test_feishu_register_inventory_sku_unit(self):
        """飞书「库存SKU」取价格表，WB 发货订单留空。"""
        from types import SimpleNamespace
        from unittest import mock
        from sellerops.features.orders import feishu_register as feishu_register
        from sellerops.persistence.price_book import SHIPPING_MODE_MABANG, SHIPPING_MODE_WB
        order = {'id': '421000001', 'shopIdText': '9352', 'platformOrderId': '6060000001', 'paidTime': '2026-10-19 09:00:00', 'order_ellipsis_other_title': '商品编号*数量:BCS-AAAA-100000001*1', 'order_ellipsis_title': '商品编号*数量:BCS-WRNG-999*1'}
        args = SimpleNamespace(scope='pending', days=1, page_size=100, no_pending=True, date='', begin='', end='')
        cases = [(SHIPPING_MODE_MABANG, set(), 'SKU-LOCAL-1'), (SHIPPING_MODE_WB, set(), None),
                 (SHIPPING_MODE_MABANG, {order['id']}, None)]
        vc = 'BCS-AAAA-100000001'
        with mock.patch.object(feishu_register, 'get_mabang_cred',
                                  return_value={'shop_map': {'9352': '袁州1(9352)'}}), \
                mock.patch.object(feishu_register, 'fetch_pending_orders', return_value=[order]), \
                mock.patch.object(feishu_register.ProductSnapshotRepository, 'load_snapshot_document', return_value=None), \
                mock.patch('sellerops.integrations.wildberries.client.fetch_canceled_ids', return_value=set()):
            for mode, group_shipping_ids, expected in cases:
                with self.subTest(mode=mode, group_shipping_ids=group_shipping_ids), \
                        mock.patch.object(feishu_register, 'mabang_load_local_mapping',
                                          return_value=({vc: '测试品'}, {vc: 'SKU-LOCAL-1'}, {vc: mode})):
                    candidates, _ = feishu_register.build_records(args, group_shipping_ids=group_shipping_ids)
                    [(record, reasons)] = candidates
                    self.assertEqual(record['库存SKU'], expected)
                    self.assertEqual(feishu_register.WB_SHIPPING in reasons, expected is None)

    def test_feishu_register_counts_only_pending_rows_for_pending_blanks(self):
        from contextlib import redirect_stderr, redirect_stdout
        import csv
        from io import StringIO
        import json
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from sellerops.cli.main import main
        from sellerops.features.orders import feishu_register as register
        from sellerops.persistence.price_book import SHIPPING_MODE_WB
        from sellerops.settings import paths

        def order(local_id, platform_id, vc="", shop="1"):
            return {"id": local_id, "platformOrderId": platform_id, "shopIdText": shop,
                    "paidTime": "2026-11-01 13:05:00",
                    "order_ellipsis_other_title": f"数量:{vc}*1" if vc else ""}

        wb_vc = "BCS-WBBB-123"
        orders = [order("a", "new", wb_vc), order("b", "new"), order("c", "registered", "BCS-NAME-456"),
                  order("d", "registered"), order("e", "outside", shop="2"), order("f", "canceled", wb_vc)]
        self.addCleanup(paths.configure, paths.current.data_dir)
        for apply in (False, True):
            out, err = StringIO(), StringIO()
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as temporary, \
                    redirect_stdout(out), redirect_stderr(err), \
                    patch.object(register, 'get_mabang_cred', return_value={'shop_map': {'1': '示例店(1)'}}), \
                    patch.object(register, 'fetch_pending_orders', return_value=orders), \
                    patch.object(register.ProductSnapshotRepository, 'load_snapshot_document', return_value=None), \
                    patch('sellerops.integrations.wildberries.client.fetch_canceled_ids', return_value={'canceled'}), \
                    patch.object(register, 'mabang_load_local_mapping',
                                 return_value=({wb_vc: '示例品'}, {wb_vc: 'SKU1'}, {wb_vc: SHIPPING_MODE_WB})), \
                    patch.object(register, 'resolve_base', return_value='private-fixture-base'), \
                    patch.object(register, 'resolve_table', return_value='fixture-table'), \
                    patch.object(register, 'list_records', return_value=[{'订单编号': 'registered'}]), \
                    patch.object(register, 'batch_create', side_effect=lambda base, table, records: len(records)) as create:
                args = ['--data-dir', temporary, 'order', 'register', '--scope', 'pending',
                        '--url', 'https://fixture.invalid', '--json']
                self.assertEqual(main(args + (['--apply'] if apply else [])), 0)
                data = json.loads(out.getvalue())['data']
                self.assertEqual([data[key] for key in ('scanned_rows', 'candidate_rows', 'existing_rows',
                                                        'duplicate_rows', 'pending_orders', 'written_orders')],
                                 [6, 4, 2, 1, 1, int(apply)])
                self.assertEqual(data['excluded_rows'], {'非目标店铺': 1, '已取消': 1})
                self.assertEqual(list(data['pending_blank_counts'].values()), [1, 0, 0])
                self.assertEqual(list(data['candidate_blank_counts'].values()), [1, 1, 2])
                self.assertEqual(data['table_scan']['total'], 1)
                self.assertNotIn('private-fixture-base', out.getvalue() + err.getvalue())
                self.assertIn('[本次待登记] 1 单', err.getvalue())
                with Path(data['artifacts']['csv']).open(encoding='utf-8-sig', newline='') as file:
                    csv_rows = list(csv.DictReader(file))
                self.assertEqual([row['结果'] for row in csv_rows],
                                 ['已写入' if apply else '待写入', '跳过-本批重复', '跳过-已登记', '跳过-已登记'])
                if apply:
                    create.assert_called_once()
                    [fields] = create.call_args.args[2]
                    self.assertEqual(fields['订单编号'], 'new')
                    self.assertIsNone(fields['库存SKU'])
                    self.assertEqual(set(fields), set(register.CSV_FIELDS[:-1]) | {'商品链接'})
                else:
                    create.assert_not_called()

    def test_feishu_register_has_zero_pending_blanks_with_no_candidates_or_all_registered(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        from unittest.mock import patch
        from sellerops.features.orders import feishu_register as register

        existing = {'ids': {'history'}, 'total': 1, 'unique': 1, 'empty': 0, 'dup_ids': {}, 'dup_rows': 0}
        candidate = ({'订单编号': 'history'}, [register.MISSING_VC])
        for candidates in ([], [candidate]):
            args = SimpleNamespace(url='https://fixture.invalid', table='Orders', apply=True)
            with self.subTest(candidates=candidates), redirect_stdout(StringIO()), \
                    patch.object(register, 'resolve_base'), patch.object(register, 'resolve_table'), \
                    patch.object(register, 'build_records', return_value=(candidates, {'已取消': 1})), \
                    patch.object(register, 'scan_order_ids', return_value=existing) as scan, \
                    patch.object(register, 'write_csv'), patch.object(register, 'batch_create') as create:
                self.assertEqual(register.run(args), 0)
            self.assertEqual(scan.call_count, len(candidates))
            self.assertEqual(args.output['pending_orders'], 0)
            self.assertEqual(sum(args.output['pending_blank_counts'].values()), 0)
            self.assertEqual(args.output['existing_rows'], len(candidates))
            self.assertEqual(args.output['scanned_rows'], len(candidates) + 1)
            create.assert_not_called()

    def test_order_process_help_uses_new_order_wording(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from sellerops.cli.main import build_parser

        for command in (['order'], ['order', 'process']):
            output = StringIO()
            with self.subTest(command=command), redirect_stdout(output), self.assertRaises(SystemExit) as exc:
                build_parser().parse_args([*command, '--help'])
            self.assertEqual(exc.exception.code, 0)
            self.assertIn('处理新增订单', output.getvalue())

    def test_mabang_process_skips_feishu_when_address_or_cli_is_missing(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        from unittest.mock import patch
        from sellerops.cli.main import build_parser
        from sellerops.features.orders import mabang_process as pipeline
        from sellerops.features.orders.matching import MatchResult
        from sellerops.integrations.feishu import tables

        cases = [
            (' ', '', 'feishu.base_url 缺失'),
            ('https://fixture.invalid', '', '未检测到 lark-cli'),
            ('https://fixture.invalid', '?:/missing-lark-cli.cmd', '未检测到 lark-cli'),
        ]
        for url, configured_cli, reason in cases:
            args = build_parser().parse_args(['order', 'process'])
            cred = SimpleNamespace(feishu_base_url=lambda: url)
            output = StringIO()
            with self.subTest(reason=reason, cli=configured_cli), redirect_stdout(output), \
                    patch.dict(os.environ, SELLEROPS_LARK_CLI=configured_cli), \
                    patch.object(tables.shutil, 'which', return_value=None), \
                    patch.object(pipeline.credentials, 'get', return_value=cred), \
                    patch.object(pipeline, 'match_orders', return_value=MatchResult()), \
                    patch.object(pipeline, 'mabang_run_forecast', return_value=0) as forecast, \
                    patch.object(pipeline, 'feishu_register_run') as register:
                self.assertEqual(pipeline.run(args), 0)
                forecast.assert_called_once()
                register.assert_not_called()
                self.assertIn(f'{reason}，跳过飞书登记', output.getvalue())
                self.assertIn('[完成] 处理新增订单耗时', output.getvalue())

    def test_mabang_process_uses_configured_feishu_address_and_passes_registration_options(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        from unittest.mock import patch
        from sellerops.cli.main import build_parser
        from sellerops.features.orders import mabang_process as pipeline
        from sellerops.features.orders.matching import MatchResult

        args = build_parser().parse_args(['order', 'process', '--apply', '--table', '测试表', '--no-pending'])
        cred = SimpleNamespace(feishu_base_url=lambda: ' https://fixture.invalid ')
        output = StringIO()
        with redirect_stdout(output), \
                patch.object(pipeline.credentials, 'get', return_value=cred), \
                patch.object(pipeline, 'match_orders', return_value=MatchResult(group_ids={'fixture-order'})), \
                patch.object(pipeline, 'mabang_run_forecast', return_value=0), \
                patch.object(pipeline, 'find_lark_cli', return_value='fixture-cli'), \
                patch.object(pipeline, 'feishu_register_run', return_value=0) as register:
            self.assertEqual(pipeline.run(args), 0)
        self.assertIn('步骤 ③ 新增订单飞书登记', output.getvalue())
        self.assertIn('[完成] 处理新增订单及飞书登记耗时', output.getvalue())
        register.assert_called_once_with(args, group_shipping_ids={'fixture-order'})
        self.assertEqual((args.url, args.table, args.no_pending), ('https://fixture.invalid', '测试表', True))

    def test_promo_gap_feishu_register_parsing_and_window(self):
        from unittest.mock import patch
        from sellerops.features.reports import feishu_vc_stats as st

        fields = {'日期': '2026-09-22T03:26:00.000+08:00', '订单编号': 'fixture-order',
                  '店铺': ['示例店'], 'BCS编号': 'BCS-TEST-1', '商品中文名': '测试品', '订单量': 3}
        records = [{'fields': fields}, {'fields': {'日期': '2026-09-21'}},
                   {'fields': {'日期': '2026-09-22'}}, {}]
        with patch.object(st, 'resolve_base', return_value='fixture-base'), \
                patch.object(st, 'resolve_table', return_value='fixture-table'), \
                patch.object(st, 'list_records', return_value=records):
            result = st.vc_order_stats(date='2026-9-22', url='https://fixture.invalid')
        self.assertEqual((result['begin'], result['end']), ('2026-09-22', '2026-09-22'))
        self.assertEqual(result['raw_rows'][0], {'日期': '2026-09-22', 'order_id': 'fixture-order',
                                               'shops': ['示例店'], 'vc': 'BCS-TEST-1', 'cn': '测试品', 'qty': 3})
        self.assertEqual((result['raw_rows'][1]['vc'], result['raw_rows'][1]['qty']), ('', 1))
        self.assertEqual(result['stats'], {'总记录': 4, '无日期': 1, '日期不在窗口': 1,
                                          '店铺被过滤': 0, '无BCS编号': 1})
        with self.assertRaises(ValueError):
            st.resolve_window(end='2026-09-22')

    def test_feishu_register_chinese_name_requires_confirmed_vc(self):
        from sellerops.features.orders import feishu_register
        resolve = feishu_register._cn_resolver({'BCS-AAAA-1': '\u6d4b\u8bd5\u54c1'})
        self.assertEqual(resolve('BCS-AAAA-1'), '\u6d4b\u8bd5\u54c1')
        self.assertIsNone(resolve('BCS-AAAA-2'))
        self.assertIsNone(resolve(''))
        self.assertIsNone(resolve(None))

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
        """马帮本地映射按**表头名**定位：把列顺序彻底打乱后仍能取到正确 中文名/库存SKU（离线）。

            背景：旧实现硬编码 `r[1]/r[0]`（映射总表）与 `r[2]/r[7]`（商品价格表），
            用户插列/挪列后会静默读错——轻则全部判 NO_SKU，重则把订单替换成错误 SKU。
            """
        import os
        import shutil
        import tempfile
        from openpyxl import Workbook
        from sellerops.settings import paths as cfg
        from sellerops.features.orders import matching as mabang
        tmp = tempfile.mkdtemp(prefix='wb_hdr_')
        boss_xlsx = os.path.join(tmp, 'boss.xlsx')
        map_xlsx = os.path.join(tmp, 'map.xlsx')
        try:
            wb = Workbook()
            ws = wb.active
            ws.append(['产品图片', '库存SKU', '卖家SKU', '实际售价', '尺寸', '最低售价', 'vendorCode前缀码', '产品中文名'])
            ws.append(['img', 'TESTSKU-6161', 'S-1', '=F2*2', '1*1*1/0.1', '10', 'AAAA', '测试商品'])
            wb.save(boss_xlsx)
            wb = Workbook()
            ws = wb.active
            ws.title = '映射总表'
            ws.append(['vendorCode', '产品中文名', '实际售价'])
            ws.append(['BCS-AAAA-123', '测试商品', 10])
            wb.save(map_xlsx)
            old_boss, old_map = (cfg.PRICE_BOOK_XLSX, cfg.MAPPING_XLSX)
            cfg.PRICE_BOOK_XLSX, cfg.MAPPING_XLSX = (boss_xlsx, map_xlsx)
            try:
                vc2cn, cn2sku = mabang.load_local_mapping()
            finally:
                cfg.PRICE_BOOK_XLSX, cfg.MAPPING_XLSX = (old_boss, old_map)
            self.assertEqual(vc2cn.get('BCS-AAAA-123'), '测试商品')
            self.assertEqual(cn2sku.get('测试商品'), 'TESTSKU-6161')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

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
        with mock.patch.object(mabang, 'mb_client_get_forecast_list', side_effect=side):
            res = mabang.find_batches(None, ['ZZ'], status=3, rows_per_page=200)
        self.assertTrue(res['all_found'], '翻页后应命中后段批次')
        self.assertEqual(res['pages_fetched'], 2)
        self.assertEqual(res['found']['ZZ']['shopId'], '主号7')

    def test_feishu_register_inventory_sku_unit(self):
        """离线单测：登记「库存SKU」取本地价格表（cn2sku），绝不取马帮系统匹配值。"""
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        from sellerops.features.orders import feishu_register as feishu_register
        order = {'id': '421000001', 'shopIdText': '9352', 'platformOrderId': '6060000001', 'paidTime': '2026-10-19 09:00:00', 'order_ellipsis_other_title': '商品编号*数量:BCS-AAAA-100000001*1', 'order_ellipsis_title': '商品编号*数量:BCS-WRNG-999*1'}
        args = SimpleNamespace(scope='pending', days=1, page_size=100, no_pending=True, date='', begin='', end='')
        tmp = tempfile.mkdtemp()
        with mock.patch.object(feishu_register, 'mabang_api_get_mabang_cred', return_value={'shop_map': {'9352': '袁州1(9352)'}}), mock.patch.object(feishu_register, 'mabang_api_fetch_pending_orders', return_value=[order]), mock.patch.object(feishu_register, 'mabang_load_local_mapping', return_value=({'BCS-AAAA-100000001': '测试品'}, {'测试品': 'SKU-LOCAL-1'})), mock.patch.object(feishu_register.config, 'shop_json_path', return_value=os.path.join(tmp, 'absent.json')), mock.patch('sellerops.integrations.wildberries.client.fetch_canceled_ids', return_value=set()):
            records, _skip, _notice, _cred = feishu_register.build_records(args)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['库存SKU'], 'SKU-LOCAL-1', '库存SKU 必须来自本地价格表')
        self.assertNotEqual(records[0]['库存SKU'], 'BCS-WRNG-999', '不得回写马帮系统匹配值')

    def test_feishu_vc_stats_offline(self):
        """feishu-vc-stats 离线断言（**不联网、不调 lark-cli**）：字段解析 / 窗口 / 跨店合并 / 降序 / 前缀。

            背景：飞书「订单登记」表的 vendorCode 载体是 `BCS编号` 列（表内无「供应商代码」列）；
            同一 vendorCode 在各店 wb编号 不同，但统计须**跨店合并**到同一条（用户口径 2026-09-23）。
            """
        from datetime import datetime, timedelta
        from sellerops.features.orders import feishu_vc_stats as st
        row = st.parse_row({'日期': '2026-09-22T03:26:00.000+08:00', '订单编号': '419915682', '店铺': ['袁州1'], 'BCS编号': 'BCS-JPAV-163906301', '商品中文名': '毛球修剪器', '订单量': 3})
        self.assertEqual(row['日期'], '2026-09-22')
        self.assertEqual(row['vc'], 'BCS-JPAV-163906301')
        self.assertEqual(row['cn'], '毛球修剪器')
        self.assertEqual(row['shops'], ['袁州1'])
        self.assertEqual(row['qty'], 3)
        self.assertEqual(row['order_id'], '419915682')
        empty = st.parse_row({})
        self.assertEqual((empty['日期'], empty['vc'], empty['shops'], empty['qty']), ('', '', [], 1))
        self.assertTrue(st.in_window({'日期': '2026-09-22'}, '2026-09-22', '2026-09-22'))
        self.assertTrue(st.in_window({'日期': '2026-09-17'}, '2026-09-17', '2026-09-23'))
        self.assertFalse(st.in_window({'日期': '2026-09-16'}, '2026-09-17', '2026-09-23'))
        self.assertFalse(st.in_window({'日期': ''}, '2026-09-17', '2026-09-23'))
        b, e = st.resolve_window(days=7)
        self.assertEqual(e, time.strftime('%Y-%m-%d'))
        self.assertEqual(datetime.strptime(e, '%Y-%m-%d') - datetime.strptime(b, '%Y-%m-%d'), timedelta(days=6))
        self.assertEqual(st.resolve_window(date='2026-9-5'), ('2026-09-05', '2026-09-05'))
        self.assertEqual(st.resolve_window(begin='2026-09-20'), ('2026-09-20', '2026-09-20'))
        self.assertEqual(st.resolve_window(begin='2026-09-02', end='2026-09-05'), ('2026-09-02', '2026-09-05'))
        with self.assertRaises(ValueError):
            st.resolve_window(end='2026-09-22')
        rows = [{'日期': '2026-09-22', 'vc': 'BCS-AAAA-1', 'cn': '甲', 'shops': ['袁州1'], 'qty': 2, 'order_id': 'o1'}, {'日期': '2026-09-22', 'vc': 'BCS-AAAA-1', 'cn': '甲', 'shops': ['袁州3'], 'qty': 1, 'order_id': 'o2'}, {'日期': '2026-09-22', 'vc': 'BCS-BBBB-2', 'cn': '乙', 'shops': ['袁州2'], 'qty': 1, 'order_id': 'o3'}, {'日期': '2026-09-22', 'vc': 'BCS-CCCC-3', 'cn': '丙', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'o4'}, {'日期': '2026-09-22', 'vc': '', 'cn': '', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'o5'}]
        agg = st.aggregate(rows)
        self.assertEqual([(r['key'], r['orders'], r['qty']) for r in agg], [('BCS-AAAA-1', 2, 3), ('BCS-BBBB-2', 1, 1), ('BCS-CCCC-3', 1, 1), (st.NO_VC_KEY, 1, 1)])
        self.assertEqual([r['rank'] for r in agg], [1, 2, 3, 4])
        self.assertTrue(str(agg[-1]['key']).startswith('('))
        self.assertEqual(agg[0]['shops'], ['袁州1', '袁州3'])
        self.assertEqual(agg[0]['cn'], '甲')
        self.assertEqual([r['key'] for r in agg if r['key'] == st.NO_VC_KEY], [st.NO_VC_KEY])
        pre = st.aggregate(rows, by_prefix=True)
        self.assertEqual([(r['key'], r['orders']) for r in pre], [('AAAA', 2), ('BBBB', 1), ('CCCC', 1), (st.NO_PREFIX_KEY, 1)])
        r_shop = {'日期': '2026-09-22', 'vc': 'x', 'shops': ['袁州2']}
        self.assertTrue(st.match_shops(r_shop, None))
        self.assertTrue(st.match_shops(r_shop, set()))
        self.assertFalse(st.match_shops(r_shop, {'袁州1', '袁州3'}))
        self.assertTrue(st.match_shops(r_shop, {'袁州2'}))
        from unittest.mock import patch, Mock
        fixture = Mock()
        fixture.wb_shops.return_value = [{'shopId': 9352, 'shopName': '袁州1'}, {'shopId': 9353, 'shopName': '袁州2'}]
        with patch.object(st.credentials, 'get', return_value=fixture):
            self.assertEqual(st.resolve_shop_labels('9352'), {'袁州1'})
            self.assertEqual(st.resolve_shop_labels('袁州1,袁州3'), {'袁州1', '袁州3'})
            self.assertEqual(st.resolve_shop_labels(['9353', '璧山1']), {'袁州2', '璧山1'})
            self.assertEqual(st.resolve_shop_labels(''), set())

    def test_feishu_register_cn_fallback_unit(self):
        """feishu-register 填「商品中文名」三级回退：映射总表 → vc_known → 四位前缀码（离线）。

            2026-10-20：商品下架后 vc 从映射总表消失，登记中文名变空（74 单黑丝假发）。
            登记侧改为三级回退，仅为填名；马帮匹配/预报仍要求 vc 在映射表（在架）。
            """
        import os
        import shutil
        import tempfile
        import openpyxl
        from unittest import mock
        from sellerops.settings import paths as config
        from sellerops.features.orders import feishu_register as fr
        from sellerops.persistence.mappings import MappingRepository
        tmp = tempfile.mkdtemp()
        try:
            boss = os.path.join(tmp, 'boss.xlsx')
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = 'Sheet1'
            ws.append(['产品图片', '卖家SKU', '产品中文名', '实际售价', '尺寸', '最低售价', 'vendorCode前缀码', '库存SKU'])
            ws.append(['', 'SZLK-1', '手足裂口霜', 80, '10*10*10/0.1', 40, 'WENR', 'szlk'])
            wb.save(boss)
            known = {'BCS-BBBB-2': {'cn': '归属池品', 'dp': 88}}
            with mock.patch.object(config, 'PRICE_BOOK_XLSX', boss), mock.patch.object(MappingRepository, 'load_vc_known', return_value=known):
                resolve = fr._cn_resolver({'BCS-AAAA-1': '映射表品'})
                self.assertEqual(resolve('BCS-AAAA-1'), '映射表品')
                self.assertEqual(resolve('BCS-BBBB-2'), '归属池品')
                self.assertEqual(resolve('BCS-WENR-999'), '手足裂口霜')
                self.assertIsNone(resolve('BCS-ZZZZ-123456'))
                self.assertIsNone(resolve(''))
                self.assertIsNone(resolve(None))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

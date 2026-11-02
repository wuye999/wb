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

    def test_promo_gap_offline(self):
        """promo-gap 离线差集/分组断言（**不联网、不调 lark-cli**）。

            口径（2026-09-23 与用户确认并**修正为「合计」**）：**销量判据 = 该供应商代码在目标店铺
            （默认袁州1/2/3）的 7 天单数「合计」**，不是单店单数（同一 vc 各店 wb编号 不同，但它是同一个商品）。
            正向 gap：合计 ≥ 阈值 且某目标店未推广 → 该店该补推；
            反向 waste：合计 < 阈值 → 这些店所有「在投(status=9)」活动里的推广都建议关闭；
            nm 本地真源反查不到 vc → 「无法判定」（不推断）；暂停(11)/未识别状态 → 仅计数不逐条列。
            """
        from sellerops.features.pricing import promo_gap as pg
        from sellerops.features.pricing import promotion_rules as rules
        from sellerops.features.pricing import promotion_types as promo_types
        self.assertEqual(pg.resolve_min(None), 4)
        self.assertEqual(pg.resolve_min(''), 4)
        self.assertEqual(pg.resolve_min('6'), 6)
        for bad in (0, -3, 'abc'):
            with self.assertRaises(ValueError):
                pg.resolve_min(bad)
        all_shops = [{'shopId': 9352, 'shopName': '袁州1'}, {'shopId': 9353, 'shopName': '袁州2'}]
        self.assertEqual([x[1] for x in pg.select_shops(all_shops, '9352')], [9352])
        self.assertEqual([x[2] for x in pg.select_shops(all_shops, '袁州2')], ['袁州2'])
        self.assertEqual(len(pg.select_shops(all_shops, '9352,袁州2')), 2)
        self.assertEqual(len(pg.select_shops(all_shops, '')), 2)
        rows = [{'日期': '2026-09-22', 'vc': 'BCS-AAAA-1', 'cn': '甲', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'a1'}, {'日期': '2026-09-22', 'vc': 'BCS-AAAA-1', 'cn': '甲', 'shops': ['袁州1'], 'qty': 2, 'order_id': 'a2'}, {'日期': '2026-09-22', 'vc': 'BCS-AAAA-1', 'cn': '甲', 'shops': ['袁州2'], 'qty': 1, 'order_id': 'a3'}, {'日期': '2026-09-22', 'vc': 'BCS-AAAA-1', 'cn': '甲', 'shops': ['袁州2'], 'qty': 1, 'order_id': 'a4'}, {'日期': '2026-09-22', 'vc': 'BCS-BBBB-2', 'cn': '乙', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'b1'}, {'日期': '2026-09-22', 'vc': 'BCS-BBBB-2', 'cn': '乙', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'b2'}, {'日期': '2026-09-22', 'vc': 'BCS-BBBB-2', 'cn': '乙', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'b3'}, {'日期': '2026-09-22', 'vc': 'BCS-BBBB-2', 'cn': '乙', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'b4'}, {'日期': '2026-09-22', 'vc': 'BCS-CCCC-3', 'cn': '丙', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'c1'}, {'日期': '2026-09-22', 'vc': 'BCS-EEEE-4', 'cn': '丁', 'shops': ['袁州1', '袁州3'], 'qty': 1, 'order_id': 'e1'}, {'日期': '2026-09-22', 'vc': 'BCS-EEEE-4', 'cn': '丁', 'shops': ['袁州1', '袁州3'], 'qty': 1, 'order_id': 'e2'}, {'日期': '2026-09-22', 'vc': 'BCS-EEEE-4', 'cn': '丁', 'shops': ['袁州1', '袁州3'], 'qty': 1, 'order_id': 'e3'}, {'日期': '2026-09-22', 'vc': 'BCS-EEEE-4', 'cn': '丁', 'shops': ['袁州1', '袁州3'], 'qty': 1, 'order_id': 'e4'}, {'日期': '2026-09-22', 'vc': 'BCS-FFFF-6', 'cn': '己', 'shops': ['璧山1'], 'qty': 1, 'order_id': 'f1'}, {'日期': '2026-09-22', 'vc': '', 'cn': '', 'shops': ['袁州1'], 'qty': 1, 'order_id': 'n1'}]
        by_vc, by_shop, no_vc = rules.aggregate_orders(rows, {'袁州1', '袁州2', '袁州3'})
        self.assertEqual(by_vc['BCS-AAAA-1']['orders'], 4)
        self.assertEqual(by_vc['BCS-AAAA-1']['qty'], 5)
        self.assertEqual(by_shop['BCS-AAAA-1']['袁州1']['orders'], 2)
        self.assertEqual(by_shop['BCS-AAAA-1']['袁州2']['orders'], 2)
        self.assertEqual(by_vc['BCS-EEEE-4']['orders'], 4)
        self.assertEqual(by_shop['BCS-EEEE-4']['袁州1']['orders'], 4)
        self.assertEqual(by_shop['BCS-EEEE-4']['袁州3']['orders'], 4)
        self.assertNotIn('BCS-FFFF-6', by_vc)
        self.assertEqual(no_vc, 1)
        self.assertEqual(rules.shop_split_text(by_shop['BCS-AAAA-1']), '袁州1=2,袁州2=2')
        by1, _ = rules.shop_rows_by_vc(rows, '袁州1')
        self.assertEqual(by1['BCS-BBBB-2']['orders'], 4)
        targets2 = [({}, 9352, '袁州1'), ({}, 9353, '袁州2')]
        adverted = {9352: {'BCS-BBBB-2'}, 9353: set()}
        snaps = {9352: {'BCS-AAAA-1': {}, 'BCS-EEEE-4': {'nmId': 444}}, 9353: {'BCS-AAAA-1': {'nmID': 111}}}
        g = rules.build_gap(targets2, rows, adverted, snaps, min_orders=4)
        self.assertEqual([(x['vc'], x['shop_id']) for x in g['gap_listed']], [('BCS-AAAA-1', 9352), ('BCS-AAAA-1', 9353), ('BCS-EEEE-4', 9352)])
        self.assertEqual([(x['vc'], x['shop_id']) for x in g['gap_unlisted']], [('BCS-BBBB-2', 9353), ('BCS-EEEE-4', 9353)])
        self.assertEqual(g['gap_listed'][0]['orders'], 4)
        self.assertEqual(g['gap_listed'][0]['shop_orders'], 2)
        self.assertEqual(g['gap_listed'][0]['shop_split'], '袁州1=2,袁州2=2')
        self.assertEqual(g['gap_listed'][1]['nm_id'], 111)
        self.assertEqual(g['gap_listed'][2]['nm_id'], 444)
        self.assertIn('nmId', g['gap_listed'][0]['备注'])
        s = g['gap_stats']
        self.assertEqual((s['候选vc数'], s['已推广店位'], s['候选店铺位']), (3, 1, 5))
        self.assertEqual((s['可直接补推'], s['需先上架'], s['缺nmId']), (3, 2, 1))
        g2 = rules.build_gap(targets2, rows, {9352: set(), 9353: set()}, {9352: None, 9353: None}, min_orders=4)
        self.assertEqual(g2['gap_listed'], [])
        self.assertIn('快照', g2['gap_unlisted'][0]['备注'])
        g3 = rules.build_gap(targets2, rows, {9352: set(), 9353: set()}, snaps, min_orders=1)
        self.assertIn('BCS-CCCC-3', {x['vc'] for x in g3['gap_listed'] + g3['gap_unlisted']})
        targets3 = [({}, 9352, '袁州1'), ({}, 9353, '袁州2'), ({}, 9356, '袁州3')]

        def _prod(sid, sname, cid, status, nm, vc):
            return {'shop_id': sid, 'shop_name': sname, 'campaign_id': cid, 'campaign_name': 'c%d' % cid, 'status_id': status, 'payment_model': 'cpc', 'budget': 100, 'create_date': '', 'nm': nm, 'vc': vc, 'cn': '名%d' % nm, 'src': '店快照', 'ru': 'R%d' % nm, 'subject': '', 'fbo': 0, 'mp': 0}
        prods = [_prod(9352, '袁州1', 1, 9, 11, 'BCS-AAAA-1'), _prod(9352, '袁州1', 1, 9, 12, 'BCS-BBBB-2'), _prod(9352, '袁州1', 2, 11, 13, 'BCS-CCCC-3'), _prod(9352, '袁州1', 3, 9, 14, ''), _prod(9356, '袁州3', 4, 9, 15, 'BCS-CCCC-3'), _prod(9356, '袁州3', 5, 9, 15, 'BCS-CCCC-3'), _prod(9352, '袁州1', 6, 4, 16, 'BCS-CCCC-3')]
        w = rules.build_waste(targets3, rows, prods, min_orders=4)
        self.assertEqual([(x['shop_id'], x['campaign_id']) for x in w['waste_close']], [(9356, 4), (9356, 5)])
        self.assertTrue(all((x['verdict'] == promo_types.VERDICT_CLOSE for x in w['waste_close'])))
        self.assertEqual(w['waste_close'][0]['orders'], 1)
        self.assertEqual(w['waste_close'][0]['shop_orders'], 0)
        self.assertEqual(w['waste_close'][0]['shop_split'], '袁州1=1')
        self.assertIn('低于阈值', w['waste_close'][0]['备注'])
        self.assertNotIn(1, [x['campaign_id'] for x in w['waste_close']])
        self.assertEqual([x['campaign_id'] for x in w['waste_paused']], [2, 6])
        self.assertEqual(len(w['waste_unknown']), 1)
        self.assertEqual(w['waste_unknown_status'], [(4, 9352, 6)])
        self.assertEqual(w['waste_stats']['被推广商品位'], 7)
        self.assertEqual(w['waste_stats']['建议关闭'], 2)
        bn = w['waste_by_nm']
        self.assertEqual([(x['shop_id'], x['nm'], x['n_campaigns'], x['orders']) for x in bn], [(9356, 15, 2, 1)])
        bc = {(x['shop_id'], x['campaign_id']): x for x in w['waste_by_campaign']}
        self.assertEqual(bc[9356, 4]['被推广商品数'], 1)
        self.assertEqual(bc[9356, 4]['建议关闭数'], 1)
        self.assertTrue(bc[9356, 4]['是否整活动建议关闭'])
        self.assertEqual(bc[9352, 1]['建议关闭数'], 0)
        self.assertEqual(bc[9352, 1]['是否整活动建议关闭'], '')
        self.assertEqual(bc[9352, 2]['建议关闭数'], 0)

    def test_fbs_delivery_offline(self):
        """fbs-delivery 离线断言（**不联网**）：清单/商品解析、状态过滤、时间窗口、分页防死循环、聚合。

            数据形态取自 HAR 抓包样本（wb订单列表配送中.har / wb订单列表配送中2.har）：
            商品级状态实测分布 readyForPickup×3 / cancel×3 / shipment×1 / sold×1。
            口径（2026-10-08 与用户确认）：默认只统计 `shipment`（「待发货」）；时间基准 = 清单 createdAt；
            `orders/delivery` 返回该清单下全部订单（含已取消），必须按 state.code 过滤。
            """
        from sellerops.integrations.wildberries import supplies as sup
        from sellerops.features.orders import fbs_delivery as fd
        ps = fd.parse_supply({'supplyID': 'WB-GI-287405573', 'name': 'Заказ', 'createdAt': '2026-10-03T10:42:05Z', 'state': {'code': 'onway', 'title': '前往接待处途中'}, 'ordersCnt': 8, 'parcelsCnt': 1, 'boxesCnt': 1, 'warehouse': '莫斯科仓库', 'destinationWarehouse': {'name': 'Москва'}})
        self.assertEqual(ps['supply_id'], 'WB-GI-287405573')
        self.assertEqual(ps['created_day'], '2026-10-03')
        self.assertEqual(ps['state_code'], 'onway')
        self.assertEqual(ps['orders_cnt'], 8)
        self.assertEqual(ps['dest_name'], 'Москва')
        po = fd.parse_order({'id': 5932144473, 'nmId': 1357061934, 'article': 'BCS-OTRE-191868961', 'title': 'Шапка оверсайз', 'size': '60-62', 'price': '607 ₽', 'convertedPrice': '46.61 ¥', 'stickerID': 58617520011, 'state': {'code': 'shipment', 'title': '待发货'}, 'createdAt': '2026-10-02T05:41:25Z', 'isCanceled': False, 'isTransferred': True}, 'WB-GI-287405573')
        self.assertEqual(po['vc'], 'BCS-OTRE-191868961')
        self.assertEqual(po['nm_id'], '1357061934')
        self.assertEqual(po['state_code'], 'shipment')
        self.assertEqual(po['supply_id'], 'WB-GI-287405573')
        self.assertEqual(po['created_day'], '2026-10-02')
        self.assertTrue(po['is_transferred'])
        self.assertFalse(po['is_canceled'])
        empty = fd.parse_order({}, 'S0')
        self.assertEqual((empty['vc'], empty['state_code'], empty['order_id']), ('', '', ''))
        self.assertEqual(fd.parse_states(''), {'shipment'})
        self.assertEqual(fd.parse_states('shipment,cancel'), {'shipment', 'cancel'})
        self.assertEqual(fd.parse_states('all'), set())
        self.assertEqual(fd.parse_states('shipment', include_canceled=True), {'shipment', 'cancel'})
        self.assertTrue(fd.match_state('shipment', {'shipment'}))
        self.assertFalse(fd.match_state('sold', {'shipment'}))
        self.assertTrue(fd.match_state('sold', set()))
        self.assertTrue(fd.match_state('expectedSorting', fd.parse_states('expectedSorting')))
        self.assertTrue(fd.match_state('readyForPickup', fd.parse_states('shipment,readyForPickup')))
        self.assertEqual(fd.resolve_window(), ('', ''))
        self.assertEqual(fd.resolve_window(date='2026-10-3'), ('2026-10-03', '2026-10-03'))
        self.assertEqual(fd.resolve_window(begin='2026-10-01', end='2026-10-05'), ('2026-10-01', '2026-10-05'))
        with self.assertRaises(ValueError):
            fd.resolve_window(end='2026-10-05')
        self.assertTrue(fd.in_window('2026-10-03', '', ''))
        self.assertTrue(fd.in_window('2026-10-03', '2026-10-01', '2026-10-05'))
        self.assertFalse(fd.in_window('2026-09-30', '2026-10-01', '2026-10-05'))
        self.assertFalse(fd.in_window('', '2026-10-01', '2026-10-05'))
        seq = []

        def fake_request(session, method, url, **kw):
            if 'supplies/delivery' in url:
                seq.append(kw.get('params', {}).get('next'))
                if len(seq) == 1:
                    return {'data': {'supplies': [{'supplyID': 'S1'}], 'next': 'CUR1'}}
                return {'data': {'supplies': [{'supplyID': 'S2'}], 'next': 0}}
            return {'data': {'orders': [{'id': 1}], 'next': 0}}
        orig = sup.wb_api.request
        try:
            sup.wb_api.request = fake_request
            got = sup.iter_delivery_supplies(object(), max_pages=5)
            self.assertEqual([s['supplyID'] for s in got], ['S1', 'S2'])
            self.assertEqual(seq, [0, 'CUR1'])
            seq.clear()
            sup.wb_api.request = lambda session, method, url, **kw: {'data': {'supplies': [{'supplyID': 'X'}], 'next': 'SAME'}}
            got2 = sup.iter_delivery_supplies(object(), max_pages=9)
            self.assertEqual(len(got2), 2)
        finally:
            sup.wb_api.request = orig
        orders = [{'vc': 'BCS-AAAA-1', 'cn': '甲', 'supply_id': 'S1', 'shop_name': '袁州1', 'nm_id': '1'}, {'vc': 'BCS-AAAA-1', 'cn': '甲', 'supply_id': 'S1', 'shop_name': '袁州2', 'nm_id': '1'}, {'vc': 'BCS-BBBB-2', 'cn': '乙', 'supply_id': 'S2', 'shop_name': '袁州1', 'nm_id': '2'}, {'vc': 'BCS-CCCC-3', 'cn': fd.NO_CN, 'supply_id': 'S2', 'shop_name': '袁州1', 'nm_id': '3'}]
        agg = fd.aggregate_by_cn(orders)
        self.assertEqual([(r['cn'], r['item_count'], r['vc_count'], r['supply_count']) for r in agg], [('甲', 2, 1, 1), ('乙', 1, 1, 1), (fd.NO_CN, 1, 1, 1)])
        self.assertEqual([r['rank'] for r in agg], [1, 2, 3])
        self.assertEqual(agg[0]['shops'], ['袁州1', '袁州2'])
        self.assertTrue(str(agg[-1]['cn']).startswith('('))
        shops = [{'shopId': 9352, 'shopName': '袁州1'}, {'shopId': 9353, 'shopName': '袁州2'}]
        self.assertEqual([s['shopName'] for s in fd.select_shops(shops, '9352')], ['袁州1'])
        self.assertEqual([s['shopName'] for s in fd.select_shops(shops, '袁州2')], ['袁州2'])
        self.assertEqual(len(fd.select_shops(shops, '9352,袁州2')), 2)
        self.assertEqual(len(fd.select_shops(shops, '')), 2)

    def test_order_stats_offline(self):
        """order-stats 离线断言：解析/状态归类/窗口/聚合/百分比边界/游标翻页/Sheet 命名/落盘。

            样本形态取自 api/网络请求/wb订单动态 查询退货率.har（2026-10-27，袁州店）：
            状态分布 cancel/buyout/created/return（另有 returnDefective）；
            cursor = base64("<offset>:<时间>")，三页 50/100/150；
            ⚠ currentPeriod 实测按 **updated** 过滤（3 天窗口 65 行中仅 7 行 created 在窗口内）。
            """
        import tempfile
        from datetime import date, datetime
        from unittest import mock
        from sellerops.integrations.wildberries import order_feed as feed
        from sellerops.features.orders import order_stats as ostats
        from sellerops.features.orders import order_stats_xlsx as oxlsx
        row = ostats.parse_row({'product': {'id': 1585363941, 'name': 'Шапка зимняя', 'subjectName': '保暖帽', 'vendorCode': 'BCS-ZZMX-1534011939', 'productRating': 10}, 'order': {'id': 'eBu.r060644b97e4c4173952bc732e9922ce3.0.0', 'created': '2026-10-21T01:27:34-07:00', 'updated': '2026-10-26T21:20:26-07:00', 'status': 'buyout', 'cancelType': 'receipt', 'price': {'seller': 49.19}, 'size': {'title': 'стандарт'}, 'warehouse': {'title': 'склад продавца'}, 'destination': {'title': 'Новосибирск'}}})
        self.assertEqual(row['created_day'], '2026-10-21')
        self.assertEqual(row['order_no'], 'eBu.r060644b97e4c4173952bc732e9922ce3')
        self.assertEqual(row['vc'], 'BCS-ZZMX-1534011939')
        self.assertEqual(row['nm_id'], '1585363941')
        self.assertEqual(row['kind'], 'done')
        self.assertEqual(row['cancel_type'], 'receipt')
        self.assertEqual(row['price'], 49.19)
        empty = ostats.parse_row({})
        self.assertEqual((empty['vc'], empty['status'], empty['created_day']), ('', '', ''))
        self.assertEqual(ostats.parse_row({'order': {'id': 'x'}})['kind'], 'unknown')
        self.assertEqual(ostats.classify_status('buyout'), 'done')
        self.assertEqual(ostats.classify_status('created'), 'pending')
        for s in ('cancel', 'return', 'returnDefective', 'RETURN', 'Cancel'):
            self.assertEqual(ostats.classify_status(s), 'return', s)
        for s in ('delivered', '', None, 'sold'):
            self.assertEqual(ostats.classify_status(s), 'unknown', s)
        t = date(2026, 10, 27)
        self.assertEqual(ostats.resolve_window(today=t), ('2026-09-28', '2026-10-27'))
        self.assertEqual(ostats.resolve_window(days=1, today=t), ('2026-10-27', '2026-10-27'))
        self.assertEqual(ostats.resolve_window(begin='2026-10-01', end='2026-10-08'), ('2026-10-01', '2026-10-08'))
        self.assertEqual(ostats.resolve_window(date='2026-10-3'), ('2026-10-03', '2026-10-03'))
        self.assertEqual(ostats.resolve_window(begin='2026-10-08', end='2026-10-01'), ('2026-10-01', '2026-10-08'))
        with self.assertRaises(ValueError):
            ostats.resolve_window(end='2026-10-08')
        with self.assertRaises(ValueError):
            ostats.resolve_window(days=1, timezone='Mars/Base')
        self.assertEqual(ostats.tz_today('America/Los_Angeles', datetime(2026, 10, 27, 6, 0)), date(2026, 10, 26))
        self.assertEqual(ostats.tz_today('Asia/Shanghai', datetime(2026, 10, 27, 6, 0)), date(2026, 10, 27))
        rows = [{'cn': '感应灯', 'vc': 'A', 'shop_label': '袁州1(9352)', 'kind': 'return'}, {'cn': '感应灯', 'vc': 'B', 'shop_label': '袁州2(9353)', 'kind': 'done'}, {'cn': '感应灯', 'vc': 'B', 'shop_label': '袁州2(9353)', 'kind': 'pending'}, {'cn': '感应灯', 'vc': 'B', 'shop_label': '袁州2(9353)', 'kind': 'unknown'}, {'cn': '', 'vc': 'C', 'shop_label': '袁州1(9352)', 'kind': 'done'}, {'cn': '全是未完结', 'vc': 'D', 'shop_label': '袁州1(9352)', 'kind': 'pending'}]
        agg = ostats.aggregate_by_cn(rows, {'感应灯': 'gyd'})
        self.assertEqual([x['cn'] for x in agg], ['感应灯', '全是未完结', '(中文名未知)'])
        top = agg[0]
        self.assertEqual((top['orders'], top['returns'], top['pending'], top['unknown']), (4, 1, 1, 1))
        self.assertEqual(top['vc_count'], 2)
        self.assertEqual(top['shops'], ['袁州1(9352)', '袁州2(9353)'])
        self.assertEqual(top['stock_sku'], 'gyd')
        self.assertAlmostEqual(top['rate'], 0.25)
        self.assertAlmostEqual(top['rate_done'], 1 / 3)
        self.assertEqual([x['rank'] for x in agg], [1, 2, 3])
        pending_only = [x for x in agg if x['cn'] == '全是未完结'][0]
        self.assertIsNone(pending_only['rate_done'])
        from sellerops.features.orders import order_stats_vc as ovc
        vc_rows = [{'vc': 'B', 'nm_id': '1001', 'cn': '感应灯', 'shop_label': '袁州1(9352)', 'kind': 'return'}, {'vc': 'B', 'nm_id': '2002', 'cn': '感应灯', 'shop_label': '袁州2(9353)', 'kind': 'done'}, {'vc': 'B', 'nm_id': '2002', 'cn': '感应灯', 'shop_label': '袁州2(9353)', 'kind': 'pending'}, {'vc': 'A', 'nm_id': '9', 'cn': '感应灯', 'shop_label': '袁州1(9352)', 'kind': 'done'}, {'vc': '', 'nm_id': '7', 'cn': '', 'shop_label': '袁州1(9352)', 'kind': 'done'}, {'vc': 'D', 'nm_id': '8', 'cn': '全是未完结', 'shop_label': '袁州1(9352)', 'kind': 'pending'}]
        agg_v = ovc.aggregate_by_vc(vc_rows, {'感应灯': 'gyd'})
        self.assertEqual(ovc.NO_VC, '(无供应商代码)')
        self.assertEqual([x['vc'] for x in agg_v], ['B', 'A', 'D', ovc.NO_VC])
        v0 = agg_v[0]
        self.assertEqual((v0['orders'], v0['returns'], v0['pending'], v0['unknown']), (3, 1, 1, 0))
        self.assertEqual(v0['nm_ids'], ['1001', '2002'])
        self.assertEqual(v0['shops'], ['袁州1(9352)', '袁州2(9353)'])
        self.assertEqual((v0['cn'], v0['stock_sku']), ('感应灯', 'gyd'))
        self.assertAlmostEqual(v0['rate'], 1 / 3)
        self.assertAlmostEqual(v0['rate_done'], 0.5)
        self.assertEqual([x['rank'] for x in agg_v], [1, 2, 3, 4])
        self.assertEqual(agg_v[3]['cn'], '')
        self.assertEqual(agg_v[3]['nm_ids'], ['7'])
        self.assertIsNone([x for x in agg_v if x['vc'] == 'D'][0]['rate_done'])
        self.assertEqual(ovc.aggregate_by_vc([{'vc': 'C', 'nm_id': '1001'}, {'vc': 'C', 'nm_id': '999'}])[0]['nm_ids'], ['999', '1001'])
        self.assertEqual(ovc.aggregate_by_vc([{'vc': 'E', 'status': 'cancel'}])[0]['returns'], 1)
        bodies = []
        pages = [{'data': {'pageInfo': {'cursor': 'NTA:MjAyNi0xMC0yNyAwNzoyNToxNg'}, 'orders': [{'order': {'id': 'a.0.0'}}]}}, {'data': {'pageInfo': {'cursor': ''}, 'orders': [{'order': {'id': 'b.0.0'}}]}}]

        def fake(session, method, url, **kw):
            bodies.append(kw.get('json'))
            return pages[len(bodies) - 1]
        with mock.patch.object(feed.wb_api, 'request', side_effect=fake):
            got = list(feed.iter_orders(object(), '2026-09-28', '2026-10-27', limit=1, sleep=0))
        self.assertEqual([x['order']['id'] for x in got], ['a.0.0', 'b.0.0'])
        self.assertNotIn('cursor', bodies[0])
        self.assertEqual(bodies[1]['cursor'], 'NTA:MjAyNi0xMC0yNyAwNzoyNToxNg')
        self.assertEqual(bodies[0]['currentPeriod'], {'start': '2026-09-28', 'end': '2026-10-27'})
        self.assertEqual(bodies[0]['timezone'], 'America/Los_Angeles')
        self.assertEqual(bodies[0]['orderBy'], {'field': 'order.updated', 'mode': 'desc'})
        self.assertFalse(bodies[0]['isDefective'])
        self.assertEqual(feed.cursor_offset('NTA:MjAyNi0xMC0yNyAwNzoyNToxNg'), 50)
        with mock.patch.object(feed.wb_api, 'request', return_value={'data': {'pageInfo': {'cursor': 'SAME'}, 'orders': [{'order': {'id': 'x'}}]}}):
            self.assertEqual(len(list(feed.iter_orders(object(), 'a', 'b', max_pages=9, sleep=0))), 2)
        with mock.patch.object(feed.wb_api, 'request', return_value={'data': {'pageInfo': {'cursor': 'C'}, 'orders': []}}):
            self.assertEqual(list(feed.iter_orders(object(), 'a', 'b', sleep=0)), [])
        with mock.patch.object(feed.wb_api, 'request', return_value={'error': True, 'errorText': 'bad period'}):
            with self.assertRaises(Exception):
                list(feed.iter_orders(object(), 'a', 'b', sleep=0))
        shops = [{'shopId': 9352, 'shopName': '袁州1'}, {'shopId': 9353, 'shopName': '袁州2'}]
        self.assertEqual([s['shopId'] for s in ostats.select_shops(shops, '9352,袁州2')], [9352, 9353])
        self.assertEqual(len(ostats.select_shops(shops, '')), 2)
        self.assertEqual(ostats.select_shops(shops, '9356'), [])
        used = {oxlsx.TITLE_SUMMARY.lower(), oxlsx.TITLE_DETAIL.lower()}
        self.assertEqual(oxlsx.safe_sheet_name('袁州1/测试[1]:*?', set()), '袁州1_测试_1____')
        self.assertLessEqual(len(oxlsx.safe_sheet_name('超长' * 40, set())), 31)
        self.assertEqual(oxlsx.safe_sheet_name('汇总', used), '汇总(2)')
        self.assertEqual(oxlsx.safe_sheet_name('袁州1', used), '袁州1')
        self.assertEqual(oxlsx.safe_sheet_name('袁州1', used), '袁州1(2)')
        self.assertEqual(oxlsx.safe_sheet_name(oxlsx.TITLE_VC, {oxlsx.TITLE_VC.lower()}), f'{oxlsx.TITLE_VC}(2)')
        import openpyxl
        detail_row = {'shop_label': '袁州1(9352)', 'shop_id': 9352, 'order_id': 'a.0.0', 'order_no': 'a', 'created_day': '2026-10-21', 'created': '2026-10-21T01:27:34-07:00', 'updated': '2026-10-26T21:20:26-07:00', 'status': 'cancel', 'kind': 'return', 'cancel_type': 'receipt', 'cn': '感应灯', 'vc': 'BCS-AAAA-1', 'stock_sku': 'gyd', 'nm_id': '1', 'title': 'Лампа', 'size': 'стандарт', 'price': 49.19, 'warehouse': 'склад', 'dest': 'Москва'}
        res = {'begin': '2026-09-28', 'end': '2026-10-27', 'timezone': 'America/Los_Angeles', 'rows': [detail_row], 'by_cn': [{'rank': 1, 'cn': '感应灯', 'orders': 1, 'returns': 1, 'pending': 0, 'unknown': 0, 'vc_count': 1, 'shops': ['袁州1(9352)'], 'stock_sku': 'gyd', 'rate': 1.0, 'rate_done': None}], 'by_cn_by_shop': {'袁州1(9352)': [{'rank': 1, 'cn': '感应灯', 'orders': 1, 'returns': 1, 'pending': 0, 'unknown': 0, 'vc_count': 1, 'shops': ['袁州1(9352)'], 'stock_sku': 'gyd', 'rate': 1.0, 'rate_done': None}]}, 'by_vc': [{'rank': 1, 'vc': 'BCS-AAAA-1', 'cn': '感应灯', 'orders': 1, 'returns': 1, 'pending': 0, 'unknown': 0, 'nm_ids': ['1'], 'shops': ['袁州1(9352)'], 'stock_sku': 'gyd', 'rate': 1.0, 'rate_done': None}], 'per_shop': [{'shop': '袁州1', 'shop_id': 9352, 'label': '袁州1(9352)', 'orders': 1, 'returns': 1, 'pending': 0, 'unknown': 0, 'error': ''}], 'by_shop': [{'shop_label': '袁州1(9352)', 'orders': 1, 'returns': 1}], 'status_counter': {'cancel': 1}, 'unknown_status': {}, 'out_of_window': 3, 'failed_shops': [], 'dup_order_ids': []}
        with tempfile.TemporaryDirectory() as tmp:
            path = oxlsx.write_xlsx(res, out=os.path.join(tmp, 't.xlsx'))
            self.assertTrue(os.path.exists(path))
            book = openpyxl.load_workbook(path)
            self.assertEqual(book.sheetnames, ['汇总', '按供应商代码', '袁州1(9352)', '订单明细'])
            wsv = book[oxlsx.TITLE_VC]
            self.assertEqual([c.value for c in wsv[1]], oxlsx.VC_HEADERS)
            self.assertEqual(len(oxlsx.VC_WIDTHS), len(oxlsx.VC_HEADERS))
            self.assertEqual((wsv['A2'].value, wsv['B2'].value, wsv['C2'].value), (1, 'BCS-AAAA-1', '感应灯'))
            self.assertEqual(wsv['F2'].number_format, '0.00%')
            self.assertEqual(wsv['G2'].value, '-')
            self.assertEqual(wsv['J2'].value, '1')
            self.assertTrue(wsv['J2'].alignment.wrap_text)
            self.assertEqual((wsv['B3'].value, wsv['D3'].value), ('合计', 1))
            self.assertEqual(wsv.freeze_panes, 'D2')
            ws = book['汇总']
            self.assertEqual([c.value for c in ws[1]], oxlsx.HEADERS)
            self.assertEqual(ws['A2'].value, 1)
            self.assertEqual(ws['E2'].number_format, '0.00%')
            self.assertEqual(ws['F2'].value, '-')
            self.assertEqual(ws['B3'].value, '合计')
            self.assertEqual(ws['C3'].value, 1)
            self.assertEqual(book['订单明细'].max_row, 2)
            self.assertEqual(book['订单明细']['I2'].value, '退货')

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

    def test_vc_prefix_tail_format_mapping(self):
        """vendorCode 前缀正则须容忍「多变体序号」尾段（离线，纯正则断言）。

            背景：形如 `BCS-FZMG-334862442-2` / `BCS-ZZMX-191868961-15` 的卡（前缀后多一段 `-N`）
            此前不匹配 `config.VC_PREFIX_RE`（旧式要求「数字结尾 / 含斜杠」），导致
            `mapping.py` 前缀自动补录、`mapping_excel.py` 前缀兜底、`mapping_repo` 名称解析兜底
            **三处全部落空** → 每店恒定 55 条永久留在「未映射商品」（2026-09-21 实测：
            ZZMX 15 / FZMG 10 / GYDX 10 / MQXJ 10 / PPYD 10，三店完全同一批）。
            """
        import re
        from sellerops.settings import paths as config
        rx = config.VC_PREFIX_RE
        valid = [('BCS-CYQX-12345678', 'CYQX'), ('BCS-CYQX-ozon-card-12345678', 'CYQX'), ('BCS-QQNN-WRLINWI/1078999444', 'QQNN'), ('BCS-FZMG-334862442-2', 'FZMG'), ('BCS-ZZMX-191868961-15', 'ZZMX')]
        for vc, expect in valid:
            with self.subTest(vc=vc):
                m = re.match(rx, vc)
                self.assertIsNotNone(m, f'应匹配成功: {vc}')
                self.assertEqual(m.group(1), expect)
        invalid = ['BCS-qqnn-12345678', 'BCS-1234-12345678', 'BCS-QQN-12345678', 'BCS-QQNNN-12345678', 'BCS-QQNN-12345a', 'BCS-QQNN-12345-x', 'BCS-QQNN-', 'ABC-QQNN-12345678', 'BCS-QQNN-abc-1', '']
        for vc in invalid:
            with self.subTest(vc=vc):
                self.assertIsNone(re.match(rx, vc), f'应不匹配: {vc}')

    def test_promo_goods_parse_offline(self):
        """promo-goods 离线解析断言（**不联网**）：查询串 / cmp 域请求头 / 供应商 UUID / 展平 / 本地反查。

            背景：cmp.wildberries.ru 广告推广接口 `GET /api/v1/adverts` 首次接入，
            被推广商品内嵌在 `content[].stocks.products[]`；抓包依据
            `api/网络请求/wb推广活动列表.har`（2026-09-23，status=[4,9,11]）。
            """
        from sellerops.integrations.wildberries import advertising as ads
        from sellerops.features.pricing import adverts as adverts
        url = ads.adverts_url(1, 100)
        self.assertIn('status=%5B4,9,11%5D', url)
        self.assertIn('bid_type=%5B1,2%5D', url)
        self.assertIn('type=%5B8,9%5D', url)
        self.assertIn('show_stocks=true', url)
        self.assertIn('page_number=1', url)
        self.assertIn('page_size=100', url)
        shop = {'authorizev3': 'JWT-TEST', 'cookie': 'a=1; x-supplier-id-external=UUID-XYZ; b=2'}
        h = ads.cmp_headers(shop)
        self.assertEqual(h['authorization'], 'Bearer JWT-TEST')
        self.assertEqual(h['authorizev3'], 'JWT-TEST')
        self.assertEqual(h['x-supplierid'], 'UUID-XYZ')
        self.assertEqual(ads.supplier_uuid_from_cookie(shop), 'UUID-XYZ')
        self.assertEqual(ads.supplier_uuid_from_cookie({'cookie': 'a=1'}), '')
        campaigns = [{'id': 40370920, 'campaign_name': 'c1', 'status_id': 9, 'payment_model': 'cpc', 'budget': 100, 'create_date': '2026-09-23T06:01:11.811229+03:00', 'products_count': 2, 'stocks': {'products': [{'nm': 1358933567, 'name': 'Переноска', 'subject': {'name': 'Переноски'}, 'total_quantity_fbo': 0, 'total_quantity_mp': 998}, {'nm': 1336777003, 'name': 'Пионы', 'subject': {'id': 1156}, 'total_quantity_fbo': 1}]}}, {'id': 2, 'campaign_name': 'c2', 'products_count': 3, 'stocks': {'products': []}}, {'id': 3, 'campaign_name': 'c3', 'products_count': 0}]
        flat = adverts.iter_campaign_products(campaigns)
        self.assertEqual(len(flat), 2)
        self.assertEqual([p['nm'] for _, p in flat], [1358933567, 1336777003])
        self.assertEqual(adverts.missing_detail_campaigns(campaigns), [2])

        class _Args:
            status = '9'
            no_cn = False
            page_size = 100
            limit = 0
            max_pages = 50
        self.assertEqual(adverts.build_options(_Args())['statuses'], (9,))
        _Args.status = ''
        opt = adverts.build_options(_Args())
        self.assertEqual(opt['statuses'], ads.STATUS_DEFAULT)
        self.assertTrue(opt['with_cn'])
        from sellerops.persistence.product_index import NmResolver
        info = NmResolver(9352, with_cn=True).resolve(999999999999)
        self.assertEqual(info, {'vc': '', 'cn': '', 'src': ''})

    def test_trash_wb_move_nms_offline(self):
        """trash 的 WB 原生移回收站（moveNmsToTrash）离线断言（**不联网**）。

            抓包依据 `api/网络请求/wb批量移至回收站.har`（2026-10-29）：
            body 为裸数组 `[{"nmID": int}]`，响应 `{error, errorText, additionalErrors}`，
            **不返回成功条数** ⇒ 只能按 nmID 逐条记账。
            """
        from sellerops.core import utils as common
        from sellerops.features.listings import trash as T
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {}}, [1, 2]), {})
        self.assertEqual(T.parse_move_response({'error': True, 'errorText': 'boom'}, [1, 2]), {1: 'boom', 2: 'boom'})
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {'2': '有库存'}}, [1, 2]), {2: '有库存'})
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {'errors': [{'params': {'1': '3'}, 'trKey': 'trDelivered'}]}}, [3, 4]), {3: 'trDelivered'})
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {'globalErr': 'x'}}, [5, 6]), {5: 'globalErr:x', 6: 'globalErr:x'})
        calls = []

        def fake_post(session, url, payload, allow_400_json=True):
            calls.append((url, payload))
            return {'error': False, 'additionalErrors': {}}
        orig_ms, orig_post = (T.wb_api.make_session, T.wb_api.request_post)
        T.wb_api.make_session = lambda shop, rv=None: object()
        T.wb_api.request_post = fake_post
        try:
            r = T.move_nms({}, list(range(1, 701)), interval=0)
            self.assertEqual((r['ok'], r['fail']), (700, 0))
            self.assertEqual([len(c[1]) for c in calls], [300, 300, 100])
            self.assertTrue(calls[0][0].endswith('/source/v2/moveNmsToTrash'))
            self.assertEqual(calls[0][1][0], {'nmID': 1})
            calls.clear()
            r = T.move_nms({}, [9, 9, 8, 8, 7], chunk=1000, interval=0)
            self.assertEqual([len(c[1]) for c in calls], [3])
            self.assertEqual([d['nmID'] for d in r['details']], [9, 8, 7])
            calls.clear()

            def post_403(session, url, payload, allow_400_json=True):
                calls.append(payload)
                if len(calls) == 2:
                    raise common.CookieExpiredError('403：cookie 已失效')
                return {'error': False, 'additionalErrors': {}}
            T.wb_api.request_post = post_403
            r = T.move_nms({}, list(range(1, 651)), interval=0)
            self.assertEqual((r['ok'], r['fail']), (300, 350))
            self.assertTrue(r['aborted'])
            self.assertIn('cookie 失效', r['details'][-1]['msg'])
            calls.clear()

            def post_boom(session, url, payload, allow_400_json=True):
                raise RuntimeError('conn reset')
            T.wb_api.request_post = post_boom
            r = T.move_nms({}, [11, 12], interval=0)
            self.assertEqual((r['ok'], r['fail']), (0, 2))
            self.assertIn('conn reset', r['details'][0]['msg'])
        finally:
            T.wb_api.make_session, T.wb_api.request_post = (orig_ms, orig_post)

    def test_default_warehouse_resolution(self):
        """仓库 ID 解析（WB 原生 offices/stores/short）：仅莫斯科，无确认结果则 None（离线）。

            2026-10-29 起不再依赖 BCS，也不再读 known_warehouses.json。
            """
        from sellerops.features.listings import warehouse as wh
        orig = wh.list_stores
        try:
            wh._cache.clear()
            wh.list_stores = lambda sid: [{'id': 111, 'name': '成都仓库', 'warehouseName': 'Chengdu'}, {'id': 222, 'name': '莫斯科仓库', 'warehouseName': 'Москва (Белая дача)'}]
            self.assertEqual(wh.resolve_warehouse(9352), 222)
            wh._cache.clear()
            wh.list_stores = lambda sid: [{'id': 333, 'name': '成都仓库', 'warehouseName': 'Chengdu'}, {'id': 444, 'name': '二号仓', 'warehouseName': 'Подольск'}]
            self.assertIsNone(wh.resolve_warehouse(9352))
            wh._cache.clear()
            wh.list_stores = lambda sid: [{'id': 333, 'name': '成都仓库', 'warehouseName': 'Chengdu'}]
            self.assertIsNone(wh.resolve_warehouse(9352))
            self.assertEqual(wh._cache, {})
            wh._cache.clear()
            wh.list_stores = lambda sid: []
            self.assertIsNone(wh.resolve_warehouse(9352))
            wh._cache.clear()
            calls = []
            wh.list_stores = lambda sid: calls.append(sid) or [{'id': 222, 'name': '莫斯科仓库'}]
            wh.resolve_warehouse(9352)
            wh.resolve_warehouse(9352)
            self.assertEqual(len(calls), 1)
        finally:
            wh.list_stores = orig
            wh._cache.clear()

    def test_warehouse_client_field_trap(self):
        """WB 仓库接口字段陷阱：必须取 `id`，不能取 `warehouseId`（后者是 officeId）（离线）。

            抓包依据 api/网络请求/wb获取仓库id.har（2026-10-29）：
            {"name":"莫斯科仓库","warehouseId":211,"id":1929635} —— 用错会清到错误仓库。
            """
        from sellerops.integrations.wildberries import warehouses as wc
        capture = {}

        class _FakeSession:
            pass
        orig_ms, orig_req = (wc.wb_api.make_session, wc.wb_api.request)

        def fake_request(session, method, url, **kw):
            capture['url'] = url
            capture['params'] = kw.get('params')
            return {'error': False, 'additionalErrors': {'errorCode': ''}, 'data': [{'name': '成都仓库', 'warehouseName': '382850-Chengdu', 'warehouseId': 3006477, 'id': 1929632, 'deliveryType': 1, 'isDeleting': False}, {'name': '莫斯科仓库', 'warehouseName': 'Москва (Белая дача)', 'warehouseId': 211, 'id': 1929635, 'deliveryType': 1, 'isDeleting': False}, {'name': '删除中仓库', 'warehouseName': 'x', 'warehouseId': 9, 'id': 999999, 'deliveryType': 1, 'isDeleting': True}]}
        try:
            wc.wb_api.make_session = lambda shop, rv=None: _FakeSession()
            wc.wb_api.request = fake_request
            rows = wc.get_stores({'shopId': 9352})
            self.assertTrue(capture['url'].endswith('/portal/offices/stores/short'))
            self.assertEqual(capture['params'], {'deliveryType': 1, 'order': 'asc'})
            self.assertEqual([r['id'] for r in rows], [1929632, 1929635])
            msk = next((r for r in rows if r['name'] == '莫斯科仓库'))
            self.assertEqual(msk['id'], 1929635)
            self.assertEqual(msk['warehouseId'], 211)
            self.assertNotEqual(msk['id'], msk['warehouseId'])
        finally:
            wc.wb_api.make_session, wc.wb_api.request = (orig_ms, orig_req)

    def test_ops_channel_default(self):
        """默认通道映射：trash → wb（WB 原生）；price/stock → bcs（其 WB 通道走 price_wb/stock_wb）。"""
        from sellerops.features.listings.operations import _default_channel
        self.assertEqual(_default_channel('trash'), 'wb')
        self.assertEqual(_default_channel('price'), 'bcs')
        self.assertEqual(_default_channel('stock'), 'bcs')

    def test_price_wb_offline(self):
        """price_wb 离线断言（**不触网、不写平台**）。

            依据 HAR `api/网络请求/wb在线批量修改价格.har`：
            - 提交条目 {"vendorCode","nmID","price","currencyIsoCode":"CNY"}（只改价无 discount 字段）
            - checkChange=true 预检（可能返回 priceModal/quarantineModal）→ checkChange=false 落库
              =「自动确认提交」（用户确认的语义，与改折扣两阶段同机制）
            """
        from unittest import mock
        from argparse import Namespace
        from sellerops.integrations.wildberries import client as wc
        from sellerops.features.listings import prices as price_wb
        shop = {'shopId': 9352, 'shopName': '袁州1', 'cookie': '', 'authorizev3': 't', 'wb_seller_lk': 't'}
        it = {'vc': 'BCS-X-1', 'nmID': 123, 'price': 150, 'cur_price': 198.0, 'orig_zero': False, 'discount': None, 'clubDiscount': None, 'cn': '测试'}
        p0 = price_wb._build_payload(it, None)
        self.assertEqual(p0, {'vendorCode': 'BCS-X-1', 'nmID': 123, 'price': 150, 'currencyIsoCode': 'CNY'})
        self.assertEqual(price_wb._build_payload(it, 30)['discount'], 30)
        self.assertEqual(price_wb._build_payload(it, 0)['discount'], 0)
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((url.split('?')[-1], kwargs.get('json')))
            if url.endswith('checkChange=true'):
                return {'data': {'priceModal': True, 'quarantineModal': True}, 'error': False, 'errorText': ''}
            return {'data': {'id': 166628902, 'alreadyExists': False}, 'error': False, 'errorText': ''}
        items = [{'vc': f'BCS-WB-{i}', 'nmID': 1000 + i, 'price': 150, 'cur_price': 198.0, 'orig_zero': False, 'discount': None, 'clubDiscount': None, 'cn': '测试'} for i in range(4)]
        plan = {'shopId': 9352, 'items': items}
        pargs = Namespace(chunk=2, interval=0, discount=None)
        client = wc.WBClient(shop, root_version='v1.113.3')
        results, ts = ([], '2026-09-24 12:00:00')
        with mock.patch.object(wc, 'request', side_effect=fake_request):
            ok, fail = price_wb.exec_shop(client, plan, pargs, results, ts)
        self.assertEqual((ok, fail), (4, 0))
        self.assertEqual([c[0] for c in calls], ['checkChange=true', 'checkChange=false', 'checkChange=true', 'checkChange=false'])
        self.assertEqual(calls[0][1], {'data': [{'vendorCode': 'BCS-WB-0', 'nmID': 1000, 'price': 150, 'currencyIsoCode': 'CNY'}, {'vendorCode': 'BCS-WB-1', 'nmID': 1001, 'price': 150, 'currencyIsoCode': 'CNY'}]})
        self.assertEqual(len(results), 4)
        self.assertTrue(all((r[2] == 'price_wb' for r in results)))

        def fake_request_exists(session, method, url, **kwargs):
            if url.endswith('checkChange=true'):
                return {'data': {'priceModal': False, 'quarantineModal': False}, 'error': False, 'errorText': ''}
            return {'data': {'id': 166628902, 'alreadyExists': True}, 'error': False, 'errorText': ''}
        witems = [{'vendorCode': f'BCS-WB-{i}', 'nmID': 2000 + i, 'price': 88} for i in range(3)]
        calls.clear()
        with mock.patch.object(wc, 'request', side_effect=fake_request_exists):
            r = price_wb.apply_prices(shop, witems, root_version='v1.113.3', chunk=2, interval=0)
        self.assertEqual((r['ok'], r['fail']), (3, 0))
        self.assertEqual(r['task_ids'], [166628902, 166628902])
        self.assertEqual(len(r['details']), 3)
        self.assertTrue(all((d['ok'] for d in r['details'])))
        bad_items = [{'vc': 'BCS-WB-9', 'nmID': 9999, 'price': 99, 'cur_price': 198.0, 'orig_zero': False, 'discount': None, 'clubDiscount': None, 'cn': '测试'}]
        plan_bad = {'shopId': 9352, 'items': bad_items}
        calls.clear()
        with mock.patch.object(wc, 'request', side_effect=fake_request):
            ok2, fail2 = price_wb.exec_shop(client, plan_bad, pargs, [], ts)
        self.assertEqual((ok2, fail2), (0, 1))  # rejected target prices must surface as failures
        self.assertEqual(calls, [])

    def test_price_boss_dp_formula_unit(self):
        """price 目标价：商品价格表 D 列「实际售价」公式解析 + floor 口径（离线）。

            覆盖 2026-10-20 口径变更：D 列由「双倍售价」改为「实际售价」，
            不再假设 =F*2；需支持 =F2*2 / =F2 / =$F$2*2 / =ROUND(...) / 纯数值，
            无法解析时回退 Excel 缓存值，两者皆无则 None 并告警。
            """
        import contextlib
        import io as _io
        import os
        import shutil
        import tempfile
        from unittest import mock
        import openpyxl
        from sellerops.settings import paths as config
        from sellerops.features.listings import plans as ops_plan
        from sellerops.persistence.mappings import MappingRepository
        header = ['产品图片', '卖家SKU', '产品中文名', '实际售价', '尺寸', '最低售价', 'vendorCode前缀码', '库存SKU']
        rows = [['', 'SKU-1', '旧格式', '=F2*2', '10*10*10/0.1', 40.25, 'AAAA', 's1'], ['', 'SKU-2', '无乘数', '=F3', '10*10*10/0.1', 41.525, 'BBBB', 's2'], ['', 'SKU-3', '绝对引用', '=$F$4*2', '10*10*10/0.1', 39.775, 'CCCC', 's3'], ['', 'SKU-4', 'ROUND', '=ROUND(F5*1.3,2)', '10*10*10/0.1', 46.2, 'DDDD', 's4'], ['', 'SKU-5', '走缓存', '=VLOOKUP(A2,A:F,6,0)', '10*10*10/0.1', 50, 'EEEE', 's5'], ['', 'SKU-6', '纯数值', 100.75, '10*10*10/0.1', 60, 'FFFF', 's6'], ['', 'SKU-7', '无缓存', '=IF(A2>0,1,2)', '10*10*10/0.1', 70, 'GGGG', 's7']]
        wb_formula = openpyxl.Workbook()
        ws = wb_formula.active
        ws.title = 'Sheet1'
        ws.append(header)
        for r in rows:
            ws.append(r)
        wb_cached = openpyxl.Workbook()
        ws2 = wb_cached.active
        ws2.title = 'Sheet1'
        ws2.append(header)
        for r in rows:
            ws2.append([r[0], r[1], r[2], None, r[4], r[5], r[6], r[7]])
        ws2.cell(2, 4).value = 80.5
        ws2.cell(3, 4).value = 41.525
        ws2.cell(4, 4).value = 79.55
        ws2.cell(5, 4).value = 60.06
        ws2.cell(6, 4).value = 88.88
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'boss.xlsx')
            wb_formula.save(path)
            err = _io.StringIO()
            with mock.patch('openpyxl.load_workbook', side_effect=lambda p, data_only=False, **kw: wb_cached if data_only else wb_formula), mock.patch.object(config, 'PRICE_BOOK_XLSX', path), contextlib.redirect_stderr(err):
                boss = MappingRepository.load_price_book()
            self.assertEqual([b['dp'] for b in boss], [80.5, 41.525, 79.55, 60.06, 88.88, 100.75, None])
            self.assertEqual([b['floor'] for b in boss], [80, 41, 79, 60, 88, 100, None])
            self.assertIn('缓存', err.getvalue())
            self.assertIn('无法解析', err.getvalue())
            self.assertEqual(ops_plan.target_price('VC-X', {'VC-X': {'cn': '无', 'dp': 88.9}}, [], None), 88)
            self.assertIsNone(ops_plan.target_price('VC-Y', {'VC-Y': {'cn': '无', 'dp': 'bad'}}, [], None))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stock_wb_offline(self):
        """stock-wb 离线断言（**不触网、不写平台**）：

            依据 HAR `api/网络请求/wb在线加载修改库存.har`：
            - 修改端点 POST .../api/v3/portal/stocks/{warehouseId}，body {"data":[{chrtId,amount}]}
            - 数组批量（探针 _scratch/probe_stock_wb_capacity.py 实测 1000 条 error=false）
            - 查询端点 GET 同域 portal/stocks?order=asc[&stores=][&search=]（data.next 游标分页）
            """
        from unittest import mock
        from sellerops.integrations.wildberries import stocks as wsc
        from sellerops.features.listings import stocks as stock_wb
        shop = {'shopId': 9352, 'shopName': '袁州1', 'cookie': '', 'authorizev3': 't', 'wb_seller_lk': 't'}
        posts = []

        def fake_request_post(session, url, payload, allow_400_json=False):
            posts.append((url, payload, allow_400_json))
            return {'error': False, 'errorText': '', 'data': {}}
        with mock.patch.object(wsc.wb_api, 'request_post', side_effect=fake_request_post):
            r = wsc.post_stocks(object(), 1929635, [{'chrtId': 2102520529, 'amount': 30}])
        self.assertTrue(posts[0][0].endswith('/api/v3/portal/stocks/1929635'))
        self.assertEqual(posts[0][1], {'data': [{'chrtId': 2102520529, 'amount': 30}]})
        self.assertTrue(posts[0][2])
        self.assertFalse(r['error'])
        items = [{'chrtId': 1000 + i, 'amount': i} for i in range(201)]
        posts.clear()
        with mock.patch.object(wsc.wb_api, 'request_post', side_effect=fake_request_post), mock.patch.object(wsc.wb_api, 'make_session', return_value=object()):
            r2 = stock_wb.set_stock(shop, 1929635, items, chunk=200, interval=0)
        self.assertEqual([len(p[1]['data']) for p in posts], [200, 1])
        self.assertEqual((r2['ok'], r2['fail']), (201, 0))
        self.assertEqual(len(r2['details']), 201)
        self.assertTrue(all((d['ok'] for d in r2['details'])))
        items_1001 = [{'chrtId': 2000 + i, 'amount': i} for i in range(1001)]
        posts.clear()
        with mock.patch.object(wsc.wb_api, 'request_post', side_effect=fake_request_post), mock.patch.object(wsc.wb_api, 'make_session', return_value=object()):
            r3 = stock_wb.set_stock(shop, 1929635, items_1001, chunk=5000, interval=0)
        self.assertEqual([len(p[1]['data']) for p in posts], [1000, 1])
        self.assertEqual(r3['ok'], 1001)

        def fake_post_fail(session, url, payload, allow_400_json=False):
            return {'error': True, 'errorText': 'Недостаточно прав', 'data': {}}
        with mock.patch.object(wsc.wb_api, 'request_post', side_effect=fake_post_fail), mock.patch.object(wsc.wb_api, 'make_session', return_value=object()):
            r4 = stock_wb.set_stock(shop, 1929635, items[:3], chunk=200, interval=0)
        self.assertEqual((r4['ok'], r4['fail']), (0, 3))
        self.assertEqual(r4['details'][0]['msg'], 'Недостаточно прав')
        pages = [{'data': {'next': 'CUR1', 'stocks': [{'article': 'BCS-A-1', 'chrtId': 11, 'amount': 1}]}}, {'data': {'next': '', 'stocks': [{'article': 'BCS-B-2', 'chrtId': 22, 'amount': 0}]}}]
        gets = []

        def fake_request(session, method, url, **kwargs):
            gets.append(kwargs.get('params') or {})
            return pages[len(gets) - 1]
        with mock.patch.object(wsc.wb_api, 'request', side_effect=fake_request):
            got = wsc.get_stocks(object(), store_id=1929635, search='BCS-A-1')
        self.assertEqual([g['chrtId'] for g in got], [11, 22])
        self.assertEqual(gets[0].get('stores'), 1929635)
        self.assertEqual(gets[0].get('search'), 'BCS-A-1')
        self.assertNotIn('next', gets[0])
        self.assertEqual(gets[1].get('next'), 'CUR1')
        self.assertEqual(gets[1].get('search'), 'BCS-A-1')

    def test_shelve_old_basket_path_unit(self):
        """离线单测：basket 分片表对齐官方插件 65 区间；vol 越界回退表尾分片（不再 768 外推）。

            背景（2026-10-19）：旧表只到 `24965→60` 且越界用 768 步长外推，vol>24965 会算出
            不存在的 61/62… 分片（card.json / 图片全 404）。官方 1.3.4 扩到 65 区间且越界回退表尾。
            """
        from sellerops.integrations.wildberries import client as wc
        self.assertEqual(len(wc._BASKET_TABLE), 65)
        self.assertEqual(wc._BASKET_TABLE[-1], (28805, '65'))
        cases = {1931583426: 'https://basket-53.wbbasket.ru/vol19315/part1931583/1931583426', 1457749715: 'https://basket-47.wbbasket.ru/vol14577/part1457749/1457749715', 1939999999: 'https://basket-53.wbbasket.ru/vol19399/part1939999/1939999999', 2900000000: 'https://basket-65.wbbasket.ru/vol29000/part2900000/2900000000'}
        for nm, want in cases.items():
            self.assertEqual(wc.basket_base(nm), want, f'nm={nm}')

    def test_shelve_file_dp_floor_unit(self):
        """shelve --file：CSV 上架价按商品价格表「实际售价」floor（离线）。

            口径 2026-10-20：D 列改为实际售价后，`shelve --file` 与 `price`/主链路口径统一为
            floor(实际售价)——此前该分支直接 float(dp) 不取整（88.9 → 88.9，现 → 88）。
            """
        import os
        import shutil
        import tempfile
        from unittest import mock
        import openpyxl
        from sellerops.settings import paths as config
        from sellerops.features.listings import publish_inputs as shelve_common
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Sheet1'
        ws.append(['产品图片', '卖家SKU', '产品中文名', '实际售价', '尺寸', '最低售价', 'vendorCode前缀码', '库存SKU'])
        ws.append(['', 'SKU-9', '测试品', 88.9, '10*10*10/0.1', 40, 'TEST', 's9'])
        tmp = tempfile.mkdtemp()
        try:
            boss_path = os.path.join(tmp, 'boss.xlsx')
            wb.save(boss_path)
            csv_path = os.path.join(tmp, 'orders.csv')
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                f.write('商品中文名,现在商品的nmId,VC\n测试品,123456,OTHER-1\n')
            with mock.patch('openpyxl.load_workbook', return_value=wb), mock.patch.object(config, 'PRICE_BOOK_XLSX', boss_path):
                items, _raw = shelve_common.load_publish_file(csv_path)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['price'], 88)
            self.assertIsInstance(items[0]['price'], int)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merge_dp_header_new_unit(self):
        """merge 读映射总表：价格列按「实际售价」表头定位（挪到非 C 列也能读到）。"""
        import io as _io
        import os
        import shutil
        import tempfile
        import openpyxl
        from unittest import mock
        from sellerops.settings import paths as config
        from sellerops.persistence import headers as xlsx_headers
        from sellerops.persistence.mappings import MappingRepository
        xlsx_headers.reset_warnings()
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'mapping.xlsx')
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = '映射总表'
            ws.append(['产品中文名', 'vendorCode', '店铺9352价格(CNY)', '折扣%', '实际售价'])
            ws.append(['测试品', 'BCS-AAAA-1', 100, 10, 88.9])
            wb.save(path)
            with mock.patch.object(config, 'MAPPING_XLSX', path), mock.patch.object(MappingRepository, 'load_vc_override', return_value={}):
                state, _ex = MappingRepository.load_mapping_state()
            self.assertEqual(state['BCS-AAAA-1']['dp'], 88.9)
            self.assertEqual(state['BCS-AAAA-1']['shop_price'], 100)
            self.assertEqual(state['BCS-AAAA-1']['cn'], '测试品')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merge_dp_header_legacy_warn_unit(self):
        """merge 读映射总表：只有旧列名「双倍售价」时不崩、dp=None，并给出可见警告。"""
        import contextlib
        import io as _io
        import os
        import shutil
        import tempfile
        import openpyxl
        from unittest import mock
        from sellerops.settings import paths as config
        from sellerops.persistence import headers as xlsx_headers
        from sellerops.persistence.mappings import MappingRepository
        xlsx_headers.reset_warnings()
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'mapping_old.xlsx')
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = '映射总表'
            ws.append(['产品中文名', 'vendorCode', '双倍售价', '店铺9352价格(CNY)'])
            ws.append(['测试品', 'BCS-AAAA-2', 88.9, 100])
            wb.save(path)
            err = _io.StringIO()
            with mock.patch.object(config, 'MAPPING_XLSX', path), mock.patch.object(MappingRepository, 'load_vc_override', return_value={}), contextlib.redirect_stderr(err):
                state, _ex = MappingRepository.load_mapping_state()
            self.assertIsNone(state['BCS-AAAA-2']['dp'])
            self.assertEqual(state['BCS-AAAA-2']['shop_price'], 100)
            msg = err.getvalue()
            self.assertIn('实际售价', msg)
            self.assertIn('双倍售价', msg)
            self.assertIn('merge', msg)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merge_single_shop_dp_header_unit(self):
        """merge 读单店表：价格列按「实际售价」表头定位；旧列名 → dp=None + 警告。"""
        import contextlib
        import io as _io
        import os
        import shutil
        import tempfile
        import openpyxl
        from unittest import mock
        from sellerops.settings import paths as config
        from sellerops.persistence import headers as xlsx_headers
        from sellerops.features.catalog import single_shop as mapping_excel
        tmp = tempfile.mkdtemp()
        try:

            def build(name, dp_header):
                p = os.path.join(tmp, name)
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = '在架映射明细'
                ws.append(['产品中文名', 'vendorCode', '本店售价(CNY)', dp_header, '本店库存'])
                ws.append(['测试品', 'BCS-BBBB-1', 120, 88.9, 5])
                wb.save(p)
                return p
            p_new = build('new.xlsx', '实际售价')
            xlsx_headers.reset_warnings()
            with mock.patch.object(config, 'shop_mapping_xlsx', lambda sid, name=None: p_new):
                rows, _unmapped = mapping_excel.load_single_shop_rows(9352)
            self.assertEqual(rows['BCS-BBBB-1']['dp'], 88.9)
            self.assertEqual(rows['BCS-BBBB-1']['price'], 120)
            p_old = build('old.xlsx', '双倍售价')
            xlsx_headers.reset_warnings()
            err = _io.StringIO()
            with mock.patch.object(config, 'shop_mapping_xlsx', lambda sid, name=None: p_old), contextlib.redirect_stderr(err):
                rows_old, _u = mapping_excel.load_single_shop_rows(9352)
            self.assertIsNone(rows_old['BCS-BBBB-1']['dp'])
            self.assertIn('实际售价', err.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_importshelve_foreign_legacy_dp_raise_unit(self):
        """import-shelve 读他人表：旧列名「双倍售价」→ 直接报错；新名挪位仍可读。"""
        import os
        import shutil
        import tempfile
        import openpyxl
        from sellerops.persistence import headers as xlsx_headers
        from sellerops.features.listings import foreign_table as foreign_table
        from sellerops.persistence.headers import HeaderError
        tmp = tempfile.mkdtemp()
        try:

            def build(name, dp_header):
                p = os.path.join(tmp, name)
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = '映射总表'
                ws.append(['产品中文名', 'vendorCode', dp_header, 'WB商品码'])
                ws.append(['他人品', 'BCS-CCCC-1396678', 66.6, 1396678])
                wb.save(p)
                return p
            xlsx_headers.reset_warnings()
            with self.assertRaises((RuntimeError, HeaderError)) as ctx:
                foreign_table.load_foreign(build('old.xlsx', '双倍售价'))
            msg = str(ctx.exception)
            self.assertIn('实际售价', msg)
            self.assertIn('双倍售价', msg)
            self.assertIn('实际表头', msg)
            items, _bad, _no_nm = foreign_table.load_foreign(build('new.xlsx', '实际售价'))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['dp'], 66.6)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_price_boss_dp_column_reorder_unit(self):
        """price 读商品价格表：价格列按「实际售价」表头定位，四个读取点都位移仍正确。"""
        import os
        import shutil
        import tempfile
        import openpyxl
        from unittest import mock
        from sellerops.settings import paths as config
        from sellerops.persistence import headers as xlsx_headers
        from sellerops.persistence.headers import HeaderError
        from sellerops.features.listings import publish_inputs as shelve_common
        from sellerops.persistence.mappings import MappingRepository
        tmp = tempfile.mkdtemp()
        try:

            def build(name, headers, row):
                p = os.path.join(tmp, name)
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = 'Sheet1'
                ws.append(headers)
                ws.append(row)
                wb.save(p)
                return p
            headers = ['产品图片', '卖家SKU', '产品中文名', '尺寸', '最低售价', '实际售价', 'vendorCode前缀码', '库存SKU']
            row = ['', 'SKU-9', '测试品', '10*10*10/0.1', 40, 88.9, 'TEST', 's9']
            p_ok = build('boss_ok.xlsx', headers, row)
            xlsx_headers.reset_warnings()
            with mock.patch.object(config, 'PRICE_BOOK_XLSX', p_ok):
                boss = MappingRepository.load_price_book()
                pmap = MappingRepository.load_prefix_map()
                details = MappingRepository.load_boss_details()
                shelve_dict = shelve_common.load_publish_catalog()
            self.assertEqual(boss[0]['dp'], 88.9)
            self.assertEqual(boss[0]['floor'], 88)
            self.assertEqual(boss[0]['stock_sku'], 's9')
            self.assertEqual(pmap['TEST']['dp'], 88.9)
            self.assertEqual(details['by_cn']['测试品']['dp'], 88.9)
            self.assertEqual(details['by_cn']['测试品']['price'], 40)
            self.assertEqual(shelve_dict['测试品']['dp'], 88.9)
            headers_bad = ['产品图片', '卖家SKU', '产品中文名', '尺寸', '最低售价', 'vendorCode前缀码', '库存SKU']
            row_bad = ['', 'SKU-9', '测试品', '10*10*10/0.1', 40, 'TEST', 's9']
            p_bad = build('boss_bad.xlsx', headers_bad, row_bad)
            with mock.patch.object(config, 'PRICE_BOOK_XLSX', p_bad):
                with self.assertRaises(HeaderError):
                    MappingRepository.load_price_book()
                with self.assertRaises(HeaderError):
                    MappingRepository.load_prefix_map()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_legacy_payload_images_subject_id(self):
        from sellerops.domain.models import PublishItem as PublishItem
        from sellerops.features.listings.card_payload import build_card_payload as build_card_payload

        item = PublishItem(nm_id=248364237, price=99.0, vendor_code='BCS-CUSTOM-PAYLOAD-TEST', length=10, width=20, height=30, weight=0.5)
        card = {'imt_name': '测试商品', 'imt_id': 1, 'data': {'subject_id': 2290}}
        payload, err = build_card_payload(item, [9352], {9352: 1929635}, card, None)
        self.assertIsNone(err)
        self.assertIsNotNone(payload)
        sdata = payload['shopDatas'][0]
        self.assertTrue(len(sdata['images']) > 0, '图片链接不得为空')
        self.assertTrue(len(sdata['mainImage']) > 0, '主图链接不得为空')
        self.assertGreater(sdata['subjectId'], 0, '类目ID必须大于0')
        self.assertEqual(sdata['vendorCode'], 'BCS-CUSTOM-PAYLOAD-TEST')

    def test_legacy_payload_missing_warehouse(self):
        from sellerops.domain.models import PublishItem as PublishItem
        from sellerops.features.listings.card_payload import build_card_payload as build_card_payload
        item = PublishItem(nm_id=248364237, price=99.0, vendor_code='BCS-TEST-VC',
                           length=10, width=20, height=30, weight=0.5)
        card = {'data': {'subject_id': 100}}
        payload, err = build_card_payload(item, [99999], {}, card, None)
        self.assertIsNone(payload)
        self.assertIn('缺少对应发货仓库配置', err)

"""Offline business regression cases with synthetic fixtures."""
import os
import time
import unittest
from unittest.mock import Mock, patch


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
        from sellerops.features.marketing import adverts as adverts
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
        from sellerops.features.products import trash as T
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {}}, [1, 2]), {})
        self.assertEqual(T.parse_move_response({'error': True, 'errorText': 'boom'}, [1, 2]), {1: ('failed', 'boom'), 2: ('failed', 'boom')})
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {'2': '有库存'}}, [1, 2]), {2: ('failed', '有库存')})
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {'errors': [{'params': {'1': '3'}, 'trKey': 'trDelivered'}]}}, [3, 4]), {3: ('failed', 'trDelivered')})
        self.assertEqual(T.parse_move_response({'error': False, 'additionalErrors': {'globalErr': 'x'}}, [5, 6]), {5: ('unknown', 'globalErr:x'), 6: ('unknown', 'globalErr:x')})
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

    def test_configured_warehouse_resolution(self):
        """仓库 ID 解析（WB 原生 offices/stores/short）：按配置匹配，无确认结果则 None（离线）。

            2026-10-29 起不再依赖 BCS，也不再读 known_warehouses.json。
            """
        from sellerops.features.products import warehouse as wh
        config = patch.object(wh.credentials, 'get', return_value=Mock(wb_warehouse_keyword='莫斯科'))
        config.start()
        self.addCleanup(config.stop)
        orig = wh.list_stores
        try:
            wh.clear_cache()
            wh.list_stores = lambda sid: [{'id': 111, 'name': '成都仓库', 'warehouseName': 'Chengdu'}, {'id': 222, 'name': '莫斯科仓库', 'warehouseName': 'Москва (Белая дача)'}]
            self.assertEqual(wh.resolve_warehouse(9352), 222)
            wh.clear_cache()
            wh.list_stores = lambda sid: [{'id': 333, 'name': '成都仓库', 'warehouseName': 'Chengdu'}, {'id': 444, 'name': '二号仓', 'warehouseName': 'Подольск'}]
            self.assertIsNone(wh.resolve_warehouse(9352))
            wh.clear_cache()
            wh.list_stores = lambda sid: [{'id': 333, 'name': '成都仓库', 'warehouseName': 'Chengdu'}]
            self.assertIsNone(wh.resolve_warehouse(9352))
            self.assertEqual(wh.warehouse_ids(wh.config.current), {})
            wh.clear_cache()
            wh.list_stores = lambda sid: []
            self.assertIsNone(wh.resolve_warehouse(9352))
            wh.clear_cache()
            calls = []
            wh.list_stores = lambda sid: calls.append(sid) or [{'id': 222, 'name': '莫斯科仓库'}]
            wh.resolve_warehouse(9352)
            wh.resolve_warehouse(9352)
            self.assertEqual(len(calls), 1)
        finally:
            wh.list_stores = orig
            wh.clear_cache()

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

    def test_product_channel_defaults(self):
        from sellerops.cli.main import build_parser
        parser = build_parser()
        for action in ("trash", "price", "stock"):
            tail = ["--amount", "10"] if action == "stock" else []
            args = ["product", action, "--all-shops", "--all-products", *tail]
            self.assertEqual(parser.parse_args(args).channel, "wb")
            self.assertEqual(parser.parse_args([*args, "--channel", "bcs"]).channel, "bcs")

    def test_price_wb_offline(self):
        from types import SimpleNamespace
        from unittest import mock
        from sellerops.features.products.prices import submit_wb_prices
        from sellerops.features.products.results import ProductResult
        from sellerops.integrations.wildberries import client as wb

        with mock.patch.object(wb, 'make_session'):
            client = wb.WBClient({'shopId': 9352})
        items = [{'vc': f'BCS-WB-{i}', 'nmID': 1000 + i, 'price': 150}
                 for i in range(4)]
        plan = {'shopId': 9352, 'items': items}
        result = ProductResult('price', 'wb', SimpleNamespace(price=150))
        with mock.patch.object(wb, 'request', side_effect=[
            {'error': False, 'data': {'priceModal': True}},
            {'error': False, 'data': {'id': 166628902, 'alreadyExists': True}},
            {'error': False, 'data': {}}, {'error': False, 'data': {'id': 166628903}},
        ]) as request:
            self.assertEqual(submit_wb_prices(client, plan, 2, 0, result), (items, False))
        self.assertEqual([c.args[2].split('=')[-1] for c in request.call_args_list],
                         ['true', 'false', 'true', 'false'])
        self.assertEqual(request.call_args_list[1].kwargs['json'], {'data': [
            {'vendorCode': row['vc'], 'nmID': row['nmID'], 'currencyIsoCode': 'CNY', 'price': 150}
            for row in items[:2]]})
        self.assertEqual([r.task_id for r in result.receipts[9352]], ['166628902'] * 2 + ['166628903'] * 2)
        self.assertTrue(all(r.status == 'accepted' for r in result.receipts[9352]))
        self.assertIn('已存在', result.receipts[9352][0].message)

    def test_minimum_price_formula_and_cache(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from openpyxl import Workbook
        from sellerops.persistence.price_book import read_price_book

        book = Workbook()
        cached = Workbook()
        for workbook in (book, cached):
            workbook.active.title = 'Sheet1'
            workbook.active.append(['最低售价', '产品中文名', '基准价'])
        for formula, name, base in [
            ('=C2*2', '引用', 40.25),
            ('=$C$3', '绝对引用', 41.525),
            ('=ROUND(C4*1.3,2)', '算式', 46.2),
            ('=IF(C5>0,1,2)', '有缓存', 50),
            (100.75, '纯数值', 60),
            ('=IF(C7>0,1,2)', '无缓存', 70),
        ]:
            book.active.append([formula, name, base])
        cached.active['A5'] = 88.88
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'book.xlsx'
            book.save(path)
            errors = StringIO()
            with patch('openpyxl.load_workbook',
                       side_effect=lambda path, data_only=False: cached if data_only else book), redirect_stderr(errors):
                rows = read_price_book(str(path))
        self.assertEqual([row['minimum_price'] for row in rows], [80.5, 41.525, 60.06, 88.88, 100.75, None])
        self.assertIn('已使用 Excel 缓存值', errors.getvalue())
        self.assertIn('且无可用缓存值', errors.getvalue())

    def test_stock_wb_offline(self):
        """stock-wb 离线断言（**不触网、不写平台**）：

            依据 HAR `api/网络请求/wb在线加载修改库存.har`：
            - 修改端点 POST .../api/v3/portal/stocks/{warehouseId}，body {"data":[{chrtId,amount}]}
            - 数组批量（探针 _scratch/probe_stock_wb_capacity.py 实测 1000 条 error=false）
            - 查询端点 GET 同域 portal/stocks?order=asc[&stores=][&search=]（data.next 游标分页）
            """
        from unittest import mock
        from sellerops.integrations.wildberries import stocks as wsc
        from sellerops.features.products import stocks as stock_wb
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

    def test_publish_order_csv_uses_explicit_price_and_book_dimensions(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from catalog_fixture import write_book
        from sellerops.features.publishing.publish_inputs import load_publish_file
        from sellerops.settings import paths

        self.addCleanup(paths.configure, paths.current.data_dir)
        with TemporaryDirectory() as directory:
            paths.configure(directory)
            write_book([('测试品', 88.9, '10*10*10/0.1')], headers=('产品中文名', '最低售价', '尺寸'))
            csv_path = Path(directory) / 'orders.csv'
            csv_path.write_text('商品中文名,现在商品的nmId,VC,price\n'
                                '测试品,123456,OTHER-1,\n'
                                '测试品,123457,OTHER-2,89.5\n', encoding='utf-8-sig')
            items, _ = load_publish_file(str(csv_path))
        self.assertEqual([item['price'] for item in items], [None, 89.5])
        self.assertEqual((items[0]['length'], items[0]['weight']), (10, 0.1))

    def test_migration_reads_ownership_from_old_total_but_prices_from_main_book(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from openpyxl import Workbook
        from catalog_fixture import write_book
        from sellerops.persistence import db, migrate_legacy
        from sellerops.settings import paths

        self.addCleanup(paths.configure, paths.current.data_dir)
        with TemporaryDirectory() as tmp:
            paths.configure(tmp)
            write_book([('SK1', 'fixture', 22, 'AAAA', '')])
            book = Workbook()
            book.active.title = '\u6620\u5c04\u603b\u8868'
            book.active.append(['vendorCode', '\u4ea7\u54c1\u4e2d\u6587\u540d', '店铺售价'])
            book.active.append(['BCS-AAAA-1', 'fixture', 88.9])
            book.save(paths.current.mapping_xlsx)
            book.close()
            migrate_legacy.migrate()
            Path(paths.current.mapping_xlsx).unlink()
            result = db.resolve_vc('BCS-AAAA-1')
            self.assertEqual((result['sku'], result['minimum_price'], result['source']), ('SK1', 22, 'prefix'))

    def test_minimum_price_header_required_even_after_optional_read(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from catalog_fixture import write_book
        from sellerops.features.system.doctor import check_book
        from sellerops.persistence.headers import HeaderError
        from sellerops.persistence.mappings import MappingRepository
        from sellerops.persistence.price_book import read_price_book
        from sellerops.settings import paths

        self.addCleanup(paths.configure, paths.current.data_dir)
        with TemporaryDirectory() as directory:
            paths.configure(directory)
            book = Path(paths.current.price_book_xlsx)
            write_book([('测试品', 'TEST')], headers=('产品中文名', 'vendorCode前缀码'))
            self.assertEqual(check_book(book)['missing_headers'], ['最低售价'])
            with self.assertRaisesRegex(HeaderError, '最低售价'):
                read_price_book(str(book))
            self.assertIsNone(MappingRepository.load_prefix_map()['TEST']['minimum_price'])
            with self.assertRaisesRegex(HeaderError, '最低售价'):
                MappingRepository.load_price_book()

            write_book([('10*10*10/0.1', 88.9, '测试品', 's9', 'TEST')],
                       headers=('尺寸', '最低售价', '产品中文名', '马帮库存SKU', 'vendorCode前缀码'))
            self.assertEqual(check_book(book)['status'], 'ready')
            [row] = MappingRepository.load_price_book()
            self.assertEqual((row['minimum_price'], row['stock_sku']), (88.9, 's9'))
            self.assertEqual(MappingRepository.load_prefix_map()['TEST']['minimum_price'], 88.9)

    def test_legacy_payload_images_subject_id(self):
        from sellerops.domain.models import PublishItem as PublishItem
        from sellerops.features.publishing.card_payload import build_card_payload as build_card_payload

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
        from sellerops.features.publishing.card_payload import build_card_payload as build_card_payload
        item = PublishItem(nm_id=248364237, price=99.0, vendor_code='BCS-TEST-VC',
                           length=10, width=20, height=30, weight=0.5)
        card = {'data': {'subject_id': 100}}
        payload, err = build_card_payload(item, [99999], {}, card, None)
        self.assertIsNone(payload)
        self.assertIn('缺少对应发货仓库配置', err)

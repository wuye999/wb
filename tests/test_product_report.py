"""运营报告的离线协议、统计、完整性和工作簿测试；所有数据为合成夹具。"""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from sellerops.domain.warehouses import select_warehouse
import unittest
from unittest.mock import Mock, PropertyMock, patch

from openpyxl import load_workbook

from sellerops.core.errors import AuthenticationError, PlatformApiError, RateLimitError
from sellerops.domain.product_reports import ProductReport, ReportCatalog, ReportIssue
from sellerops.features.reports import product_report as workflow
from sellerops.features.reports import product_report_data as data
from sellerops.features.reports.product_report_analysis import analyze, return_metrics
from sellerops.features.reports.product_report_screening import screen_products
from sellerops.features.reports.product_report_xlsx import write_report
from sellerops.integrations.bcs.client import BCSClient, OpenAPIShop
from sellerops.integrations.wildberries.open_api import WBOpenAPI, OpenAPIResponseError
from sellerops.persistence.snapshots import ProductSnapshotRepository


NOW = datetime(2026, 11, 2, 12, tzinfo=data.TZ)
VC = 'BCS-TEST-123'


def feedback(fid='f1', nm=101, stars=1, vc=VC, date='2026-11-01T20:00:00Z'):
    return {'id': fid, 'productDetails': {'nmId': nm, 'supplierArticle': vc},
            'productValuation': stars, 'createdDate': date, 'text': '=1+1'}


def order(oid='o1', nm=101, status='cancel', warehouse='莫斯科仓库', date='2026-11-01T20:00:00Z'):
    return {'srid': oid, 'nmId': nm, 'createdAt': date, 'updatedAt': date,
            'status': status, 'warehouseName': warehouse, 'isMp': True}


class TestOpenAPIProtocol(unittest.TestCase):
    def client(self):
        api = WBOpenAPI('fixture-token')
        self.addCleanup(api.close)
        return api

    def test_order_snapshot_pagination_uses_actual_count_and_empty_end(self):
        api = self.client()
        responses = [{'data': {'snapshotTime': 'stamp', 'orders': [order('a')]}},
                     {'data': {'snapshotTime': 'stamp', 'orders': [order('b')]}},
                     {'data': {'snapshotTime': 'stamp', 'orders': []}}]
        with patch.object(api, '_request', side_effect=responses) as request:
            rows = api.orders(NOW - timedelta(days=30), NOW, page_size=10000)
        self.assertEqual([row['srid'] for row in rows], ['a', 'b'])
        bodies = [call.kwargs['json'] for call in request.call_args_list]
        self.assertNotIn('snapshotTime', bodies[0]['pagination'])
        self.assertEqual([body['pagination']['offset'] for body in bodies], [0, 1, 2])
        self.assertEqual(bodies[2]['pagination']['snapshotTime'], 'stamp')
        self.assertIn('selectedPeriod', bodies[0])
        self.assertNotIn('currentPeriod', bodies[0])

    def test_order_partial_and_duplicate_are_errors(self):
        for responses in ([{'data': {'orders': []}}],
                          [{'data': {'snapshotTime': 'a', 'orders': [order()]}},
                           {'data': {'snapshotTime': 'b', 'orders': []}}],
                          [{'data': {'snapshotTime': 'a', 'orders': [order(), order()]}}]):
            api = self.client()
            with patch.object(api, '_request', side_effect=responses), self.assertRaises(PlatformApiError):
                api.orders(NOW, NOW)
        api = self.client()
        with patch.object(api, '_request', return_value={'data': {'snapshotTime': 'a', 'orders': [order()]}}):
            with self.assertRaisesRegex(PlatformApiError, '上限'):
                api.orders(NOW, NOW, max_pages=1)

    def test_order_terminal_empty_snapshot_is_valid_but_full_page_is_not(self):
        api = self.client()
        with patch.object(api, '_request', return_value={'data': {'snapshotTime': '', 'orders': [order()]}}):
            self.assertEqual(len(api.orders(NOW, NOW)), 1)
            with self.assertRaises(PlatformApiError):
                api.orders(NOW, NOW, page_size=1)
        with patch.object(api, '_request', side_effect=[
            {'data': {'snapshotTime': 'fixed', 'orders': [order('first')]}},
            {'data': {'snapshotTime': '', 'orders': [order('last')]}},
        ]) as request:
            self.assertEqual(len(api.orders(NOW, NOW, page_size=2)), 2)
            self.assertEqual(request.call_args_list[1].kwargs['json']['pagination']['snapshotTime'], 'fixed')

    def test_fbs_warehouse_ids_complete_pagination_and_overlapping_date_windows(self):
        api = self.client()
        first = {'rid': 'one', 'warehouseId': 42, 'nmId': 101, 'article': VC}
        second = {'rid': 'two', 'warehouseId': 43, 'nmId': 102, 'article': VC}
        with patch.object(api, '_request', side_effect=[
            {'orders': [first], 'next': 100}, {'orders': [], 'next': 100},
            {'orders': [first, second], 'next': 200}, {'orders': [], 'next': 200},
        ]) as request:
            result = api.fbs_orders(NOW - timedelta(days=31), NOW)
        self.assertEqual([row['rid'] for row in result], ['one', 'two'])
        queries = [call.kwargs['params'] for call in request.call_args_list]
        self.assertEqual(queries[1]['next'], 100)
        self.assertEqual(queries[0]['dateTo'], queries[2]['dateFrom'])
        self.assertLessEqual(queries[0]['dateTo'] - queries[0]['dateFrom'], 28 * 86400)
        with patch.object(api, '_request', return_value={'orders': [first], 'next': 0}):
            with self.assertRaises(PlatformApiError):
                api.fbs_orders(NOW - timedelta(days=1), NOW)

    def test_feedback_reads_answered_unanswered_and_archive_deduplicated(self):
        api = self.client()
        responses = [
            {'data': {'feedbacks': [feedback()]}}, {'data': {'feedbacks': []}},
            {'data': {'feedbacks': [feedback(), feedback('f2')]}}, {'data': {'feedbacks': []}},
            {'data': {'feedbacks': [feedback('f2'), feedback('f3')]}}, {'data': {'feedbacks': []}},
        ]
        with patch.object(api, '_request', side_effect=responses) as request:
            rows = api.feedbacks(date_from=1, date_to=2)
        self.assertEqual({row['id'] for row in rows}, {'f1', 'f2', 'f3'})
        calls = request.call_args_list
        self.assertEqual(calls[0].kwargs['params']['isAnswered'], 'false')
        self.assertEqual(calls[2].kwargs['params']['isAnswered'], 'true')
        self.assertEqual(calls[1].kwargs['params']['skip'], 1)
        self.assertTrue(calls[4].args[2].endswith('/archive'))
        self.assertNotIn('dateFrom', calls[4].kwargs['params'])
        self.assertNotIn('dateTo', calls[4].kwargs['params'])

    def test_feedback_repeated_page_or_page_cap_fails(self):
        for pages in (1, 3):
            api = self.client()
            with patch.object(api, '_request', return_value={'data': {'feedbacks': [feedback()]}}):
                with self.assertRaises(PlatformApiError):
                    api.feedbacks(max_pages=pages)

    def test_transport_has_no_cookie_or_write_route_and_redacts_errors(self):
        api = WBOpenAPI('Bearer fixture-secret', 'cn')
        self.addCleanup(api.close)
        self.assertEqual(api.session.headers['Authorization'], 'fixture-secret')
        self.assertNotIn('Cookie', api.session.headers)
        with self.assertRaises(ValueError):
            api._request('marketplace-api', 'PUT', '/api/v3/warehouses')
        for status, error in [(401, AuthenticationError), (403, AuthenticationError), (429, RateLimitError)]:
            api = WBOpenAPI('fixture-secret', 'cn')
            self.addCleanup(api.close)
            with patch.object(api.session, 'request', return_value=SimpleNamespace(status_code=status)), \
                    patch.object(api, '_last', {}):
                with self.assertRaises(error) as raised:
                    api.warehouses()
                self.assertNotIn('fixture-secret', str(raised.exception))
        response = Mock(status_code=200)
        response.json.return_value = {'error': True, 'errorText': 'fixture-secret'}
        with patch.object(api.session, 'request', return_value=response), patch.object(api, '_last', {}):
            with self.assertRaises(PlatformApiError) as raised:
                api.warehouses()
            self.assertNotIn('fixture-secret', str(raised.exception))

    def test_bcs_private_pagination_preserves_keys_without_logging(self):
        client = BCSClient()
        pages = [{'code': 200, 'total': 2, 'rows': [{'id': 11, 'shopName': '甲', 'apiKey': 'secret'}]},
                 {'code': 200, 'total': 2, 'rows': [{'id': 12, 'shopName': '乙', 'apiKey': ''}]}]
        with patch.object(BCSClient, 'base_url', new_callable=PropertyMock, return_value='fixture'), \
                patch.object(client, 'get', side_effect=pages):
            result = client.fetch_open_api_shops()
        self.assertEqual([shop.id for shop in result], [11, 12])
        self.assertEqual(result[0].api_key, 'secret')
        self.assertNotIn('secret', repr(result))
        with patch.object(BCSClient, 'base_url', new_callable=PropertyMock, return_value='fixture'), \
                patch.object(client, 'get', side_effect=[pages[0], {'code': 200, 'total': 2, 'rows': []}]):
            with self.assertRaises(PlatformApiError):
                client.fetch_open_api_shops()


class TestReportRules(unittest.TestCase):
    def test_compatible_rates_split_pending_and_unknown(self):
        rows = [{'status': value} for value in ('cancel', 'return', 'returnDefective'.lower(), 'created', 'buyout', 'new-status')]
        result = return_metrics(rows)
        self.assertEqual((result['cancel'], result['returned'], result['defective'], result['unknown']), (1, 1, 1, 1))
        self.assertEqual(result['return_rate'], 3 / 6)
        self.assertEqual(result['done_rate'], 3 / 5)
        self.assertEqual(result['actual_rate'], 2 / 6)
        self.assertIsNone(return_metrics([{'status': 'created'}])['done_rate'])
        self.assertIsNone(return_metrics([])['return_rate'])

    def test_weighted_cn_and_same_vc_different_shop_nm(self):
        report = ProductReport(NOW.isoformat(), '2026-10-04', '2026-11-02')
        report.shops = [{'id': 11, 'name': '甲'}, {'id': 12, 'name': '乙'}]
        report.coverage = {sid: {'orders': '完整', 'feedbacks': '完整'} for sid in (11, 12)}
        report.snapshots = {11: [], 12: []}
        for i in range(10):
            sid, name, nm = (11, '甲', 101) if i == 0 else (12, '乙', 202)
            raw = order(str(i), nm=nm, status='cancel' if i == 0 else 'buyout')
            report.orders.append(data.order_row(raw, sid, name, {str(nm): VC},
                                               {'id': sid, 'name': '业务仓'}, lambda *args: '同一商品'))
        report.feedbacks = [data.feedback_row(feedback(nm=101), 11, '甲', lambda *args: '同一商品')]
        result = analyze(report)
        self.assertAlmostEqual(result['by_cn'][0]['return_rate'], .1)
        self.assertEqual(len(result['by_vc']), 1)
        self.assertEqual({row['nm_id'] for row in result['links']}, {'101', '202'})
        self.assertEqual([row['negative'] for row in result['links']], [1, 0])
        self.assertEqual(result['vc_feedback'][0]['negative'], 1)

    def test_timezone_windows_and_no_vc_tail_guess(self):
        start, end, feedback_start = data.windows(30, 0, NOW)
        self.assertEqual(start.date().isoformat(), '2026-10-04')
        self.assertEqual(end.date().isoformat(), '2026-11-03')
        self.assertIsNone(feedback_start)
        for days in (0, 32):
            with self.assertRaises(ValueError):
                data.windows(days, 0, NOW)
        row = data.order_row(order(date='2026-10-26T17:00:00Z'), 11, '甲', {},
                             {'id': 11, 'name': '业务仓'}, lambda *args: '测试商品')
        self.assertEqual(row['created'], '2026-10-27T01:00:00+08:00')
        self.assertEqual(row['url'], data.product_url(101))
        self.assertEqual(row['vc'], '')

    def test_only_unique_moscow_id_and_no_office_id(self):
        selected = select_warehouse([{'name': '成都仓', 'id': 9},
                                     {'name': '莫斯科仓库', 'id': 42, 'officeId': 999}], '莫斯科')
        self.assertEqual(selected['id'], 42)
        for rows in ([{'name': '成都仓', 'id': 9}],
                     [{'name': '莫斯科仓库', 'officeId': 999}],
                     [{'name': '莫斯科一', 'id': 1}, {'name': '莫斯科二', 'id': 2}]):
            with self.assertRaises(ValueError):
                select_warehouse(rows, '莫斯科')

    def test_snapshot_missing_wrong_shop_and_incomplete(self):
        from catalog_fixture import corrupt_snapshot, snapshot
        from sellerops.settings import paths

        previous = paths.current.data_dir
        self.addCleanup(paths.configure, previous)
        with tempfile.TemporaryDirectory() as directory:
            paths.configure(directory)
            self.assertIsNone(ProductSnapshotRepository.load_snapshot_document(11))
            snapshot(11, [{'vendorCode': 'fixture', 'nmId': 1}])
            for payload in ({'shopId': 12}, {'shopId': 11, 'total': 2}, {'total': 0}):
                corrupt_snapshot(11, snapshot_json=json.dumps(payload))
                with self.assertRaises(ValueError):
                    ProductSnapshotRepository.load_snapshot_document(11)
            corrupt_snapshot(11, snapshot_json='{broken')
            with self.assertRaises(ValueError):
                ProductSnapshotRepository.load_snapshot_document(11)


class TestReportWorkflow(unittest.TestCase):
    def fixture(self, *, missing=False, api_error=False, bad_warehouse=False, records=None, snapshot_rows=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('sellerops.features.catalog.shops.archive_set', return_value=set()))
        stack.enter_context(patch.object(workflow.credentials, 'get', return_value=Mock(wb_warehouse_keyword='莫斯科')))
        stack.enter_context(patch.object(workflow, 'load_report_catalog', return_value=ReportCatalog({'测试商品'}, {VC: '测试商品'})))
        accesses = [OpenAPIShop(11, '店甲', 'secret-a'), OpenAPIShop(12, '店乙', 'secret-b')]
        stack.enter_context(patch.object(workflow.bcs, 'fetch_open_api_shops', return_value=accesses))
        stack.enter_context(patch.object(workflow, 'load_records', return_value=records or {}))
        stack.enter_context(patch.object(workflow.MappingRepository, 'build_vc_resolver',
                                         return_value=(lambda vc, fallback='': '测试商品', {})))
        def snapshot(sid):
            if missing and sid == 12:
                return None
            rows = [{'nmId': 101 if sid == 11 else 202, 'vendorCode': VC}]
            if sid == 11 and snapshot_rows is not None:
                rows = snapshot_rows
            return {'shopId': sid, 'fetchedAt': '2026-11-01 12:00:00',
                    'rows': rows}
        stack.enter_context(patch.object(workflow.ProductSnapshotRepository, 'load_snapshot_document', side_effect=snapshot))
        apis = []
        for sid in (11, 12):
            api = Mock()
            api.warehouses.return_value = [{'name': '莫斯科仓库', 'id': sid * 100, 'officeId': 555}]
            api.feedbacks.return_value = [feedback(nm=101 if sid == 11 else 202),
                                          feedback('ancient', nm=101 if sid == 11 else 202, stars=3, date='2020-01-01T00:00:00Z')]
            api.orders.return_value = [order(nm=101 if sid == 11 else 202), order('other', warehouse='成都仓库'),
                                       order('old', date='2026-01-01T00:00:00Z')]
            api.fbs_orders.return_value = [
                {'rid': 'o1', 'warehouseId': sid * 100, 'nmId': 101 if sid == 11 else 202, 'article': VC},
                {'rid': 'other', 'warehouseId': 999, 'nmId': 101, 'article': VC},
            ]
            apis.append(api)
        if api_error:
            apis[0].orders.side_effect = AuthenticationError('secret-a must not leak')
            apis[0].feedbacks.side_effect = AuthenticationError('secret-a must not leak')
        if bad_warehouse:
            apis[0].warehouses.return_value = [{'name': '成都仓库', 'id': 1}]
        factory = stack.enter_context(patch.object(workflow, 'WBOpenAPI', side_effect=apis))
        return apis, factory

    def collect(self, **kwargs):
        return workflow.collect(now=NOW, **kwargs)

    def set_orders(self, api, sid, rows):
        api.orders.return_value = [row for row, _ in rows]
        api.fbs_orders.return_value = [
            {'rid': row['srid'], 'warehouseId': sid * 100, 'nmId': row['nmId'], 'article': vc}
            for row, vc in rows
        ]

    def test_collection_keeps_only_business_warehouse_and_requested_dates(self):
        apis, factory = self.fixture()
        report = self.collect()
        self.assertFalse(report.failed)
        self.assertEqual(len(report.orders), 2)
        self.assertEqual(len(report.feedbacks), 4)
        self.assertEqual(report.coverage[11]['outside_warehouse'], 1)
        self.assertEqual(report.coverage[11]['outside_created'], 1)
        self.assertEqual(factory.call_args_list[0].args, ('secret-a', 'ru'))
        self.assertEqual(report.orders_collected_from, report.order_begin)
        self.assertFalse(analyze(report)['sales'])
        self.assertTrue(all(api.close.called for api in apis))

    def test_snapshot_vc_wins_over_historical_feedback(self):
        snapshot = [{'nmId': 101, 'vendorCode': 'VC_NEW'}]
        apis, _ = self.fixture(snapshot_rows=snapshot)
        apis[0].feedbacks.return_value = [feedback(vc='VC_OLD')]
        self.set_orders(apis[0], 11, [(order(), '')])

        self.assertEqual(data.nm_vendor_map(snapshot, apis[0].feedbacks.return_value), ({'101': 'VC_NEW'}, 0))
        report = self.collect()
        self.assertEqual(report.orders[0]['vc'], 'VC_NEW')
        self.assertFalse(any(issue.source == '商品映射' for issue in report.issues))
        self.assertFalse(report.failed)

    def test_feedback_conflict_warns_and_preserves_clean_mappings(self):
        snapshot = [{'nmId': 101, 'vendorCode': VC}]
        feedbacks = [feedback('a', nm=202, vc='VC_A'), feedback('b', nm=202, vc='VC_B'),
                     feedback('c', nm=303, vc='VC_C'), feedback('d', nm=303, vc='VC_C')]
        apis, _ = self.fixture(snapshot_rows=snapshot)
        apis[0].feedbacks.return_value = feedbacks
        self.set_orders(apis[0], 11, [(order(), ''), (order('o2', nm=202), 'VC_FBS'), (order('o3', nm=303), '')])

        self.assertEqual(data.nm_vendor_map(snapshot, feedbacks), ({'101': VC, '303': 'VC_C'}, 1))
        report = self.collect()
        self.assertEqual({row['nm_id']: row['vc'] for row in report.orders if row['shop_id'] == 11},
                         {'101': VC, '202': 'VC_FBS', '303': 'VC_C'})
        self.assertEqual([issue for issue in report.issues if issue.source == '商品映射'], [
            ReportIssue('商品映射', '1个商品码存在多个VC，已跳过兜底映射', 11, severity='警告')])
        self.assertFalse(report.failed)

    def test_order_needing_conflicting_fallback_still_fails_report(self):
        apis, _ = self.fixture()
        apis[0].feedbacks.return_value = [feedback('a', nm=202, vc='VC_A'), feedback('b', nm=202, vc='VC_B')]
        self.set_orders(apis[0], 11, [(order(nm=202), '')])
        del apis[0].fbs_orders.return_value[0]['article']

        report = self.collect()
        self.assertEqual(report.orders[0]['vc'], '')
        self.assertIn(ReportIssue('商品映射', '1个商品码存在多个VC，已跳过兜底映射', 11, severity='警告'), report.issues)
        self.assertIn(ReportIssue('订单', '商品码无法映射VC（未猜测VC尾号）：1条', 11), report.issues)
        self.assertTrue(report.failed)

    def test_snapshot_conflict_does_not_fall_back_to_feedback(self):
        snapshot = [{'nmId': 101, 'vendorCode': 'VC_A', 'goodsCode': 'VC_UNUSED'},
                    {'nmId': 101, 'vendorCode': ' ', 'goodsCode': ' VC_B ', 'supplierArticle': 'VC_UNUSED'},
                    {'nmID': 303, 'goodsCode': 'VC_C'}, {'nmId': 404}]
        feedbacks = [feedback(vc='VC_A'), feedback('blank-snapshot', nm=404, vc='VC_D')]

        self.assertEqual(data.nm_vendor_map(snapshot, feedbacks), ({'303': 'VC_C'}, 1))
        self.assertEqual(data.nm_vendor_map(snapshot[:1], feedbacks[:1]), ({'101': 'VC_A'}, 0))

    def test_short_return_window_keeps_utc8_sales_boundaries_for_both_apis_and_xlsx(self):
        apis, _ = self.fixture()
        for api in apis:
            api.feedbacks.return_value = []
        self.set_orders(apis[0], 11, [
            (order('today-11', status='buyout'), VC),
            (order('first-of-7', status='buyout', date='2026-10-26T16:00:00Z'), VC),
            (order('eighth-day', status='created', date='2026-10-26T15:59:59Z'), VC),
            (order('first-of-14', status='buyout', date='2026-10-19T16:00:00Z'), VC),
            (order('before-14', status='buyout', date='2026-10-19T15:59:59Z'), VC),
            (order('after-today', status='buyout', date='2026-11-02T16:00:00Z'), VC),
            (order('old-return', status='return', date='2026-10-26T15:59:59Z'), VC),
            (order('old-cancel', status='cancel', date='2026-10-26T15:59:59Z'), VC),
        ])
        self.set_orders(apis[1], 12, [(order('today-12', nm=202, status='buyout'), VC)])
        report = self.collect(order_days=3)
        self.assertTrue(report.orders_complete)
        for api in apis:
            for call in (api.orders.call_args, api.fbs_orders.call_args):
                self.assertEqual(call.args[0].isoformat(), '2026-10-20T00:00:00+08:00')
        self.assertEqual(report.order_begin, '2026-10-31')
        self.assertEqual(report.orders_collected_from, '2026-10-20')
        self.assertEqual({row['order_id'] for row in report.orders},
                         {'today-11', 'today-12', 'first-of-7', 'eighth-day', 'first-of-14',
                          'old-return', 'old-cancel'})
        result = analyze(report)
        for key in ('by_cn', 'by_vc'):
            self.assertEqual((result[key][0]['orders'], result[key][0]['combined_returns']), (2, 0))
        self.assertEqual({row['shop_id']: row['orders'] for row in result['links']}, {11: 1, 12: 1})
        screening = screen_products(report)
        self.assertFalse(screening.high_returns)
        for row in (result['sales'][0], screening.recent[0]):
            self.assertEqual((row['orders7'], row['orders14']), (3, 5))
            self.assertEqual(set(row['source_nms'].splitlines()), {'店甲(11)：101', '店乙(12)：202'})

        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(shops='', order_days=3, feedback_days=0, wb_region='ru',
                                   json=True, details=True, out=str(Path(directory) / 'report.xlsx'))
            with patch.object(workflow, 'collect', return_value=report):
                self.assertEqual(workflow.run(args), 0)
            self.assertNotIn('feishu_complete', args.output)
            self.assertTrue(args.output['orders_complete'])
            self.assertEqual(args.output['period'], {
                'orders_from': '2026-10-31', 'orders_collected_from': '2026-10-20',
                'feedbacks_from': None, 'sales_7_from': '2026-10-27',
                'sales_14_from': '2026-10-20', 'through': '2026-11-02',
            })
            self.assertEqual(args.output['return_metrics']['orders'], 2)
            self.assertEqual(args.output['return_metrics']['return_rate'], 0)
            with open(args.out, 'rb') as stream:
                wb = load_workbook(stream)
                self.assertNotIn('飞书出单明细', wb.sheetnames)
                self.assertEqual(wb['订单明细'].max_row, 8)
                for sheet in ('近期出单筛选', '近7及14天出单', '店铺链接缺口'):
                    headers, *rows = wb[sheet].values
                    self.assertEqual([rows[0][headers.index(f'近{days}天有效出单件数')] for days in (7, 14)], [3, 5])
                coverage = dict(wb['数据覆盖说明'].values)
                self.assertIn('2026-10-20', coverage['订单采集窗口'])
                self.assertIn('2026-10-31', coverage['退货统计窗口'])
                self.assertIn('buyout', coverage['有效出单口径 A'])
                wb.close()

    def test_partial_auth_failure_continues_other_shop_and_never_zeros_unknown(self):
        self.fixture(api_error=True)
        report = self.collect()
        self.assertTrue(report.failed)
        self.assertEqual(report.coverage[11]['orders'], '未知')
        self.assertEqual(report.coverage[12]['orders'], '完整')
        self.assertNotIn('secret-a', str(report))
        first = next(row for row in analyze(report)['links'] if row['shop_id'] == 11)
        self.assertIsNone(first['orders'])
        self.assertIsNone(first['negative'])

    def test_no_moscow_stops_remote_collection_for_that_shop(self):
        apis, _ = self.fixture(bad_warehouse=True)
        report = self.collect()
        self.assertTrue(report.failed)
        apis[0].orders.assert_not_called()
        apis[0].feedbacks.assert_not_called()
        apis[0].fbs_orders.assert_not_called()
        apis[1].orders.assert_called_once()

    def test_physical_warehouse_name_uses_matching_fbs_warehouse_id(self):
        apis, _ = self.fixture()
        apis[0].orders.return_value = [order(warehouse='склад продавца Белая Дача'),
                                      order('other', warehouse='莫斯科仓库')]
        report = self.collect()
        self.assertFalse(report.failed)
        rows = [row for row in report.orders if row['shop_id'] == 11]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['warehouse_id'], 1100)
        self.assertEqual(rows[0]['warehouse_report_name'], 'склад продавца Белая Дача')
        self.assertEqual(report.coverage[11]['outside_warehouse'], 1)

    def test_missing_fbs_identity_does_not_infer_from_warehouse_name(self):
        apis, _ = self.fixture()
        apis[0].fbs_orders.return_value = []
        report = self.collect()
        self.assertTrue(report.failed)
        self.assertFalse([row for row in report.orders if row['shop_id'] == 11])
        self.assertEqual(report.coverage[11]['orders'], '部分')

    def test_missing_snapshot_and_pending_publish_are_distinct(self):
        apis, _ = self.fixture(missing=True, records={'BCS-OTHR-9': {'11': '2026-11-02 10:00:00'}})
        self.set_orders(apis[0], 11, [(order(status='buyout'), VC),
                                     (order('other-vc', nm=909, status='created'), 'BCS-OTHR-9')])
        report = self.collect()
        self.assertTrue(report.failed)
        gaps = analyze(report)['gaps']
        self.assertEqual({row['state'] for row in gaps if row['shop_id'] == 12}, {'缺少快照无法判断'})
        pending = next(row for row in gaps if row['shop_id'] == 11 and row['vc'] == 'BCS-OTHR-9')
        self.assertIn('已有提交记录', pending['state'])
        self.assertEqual(len(gaps), 4)  # 有差评的VC仍保留。

    def test_feedback_local_date_filter_applies_to_archive(self):
        self.fixture()
        report = self.collect(feedback_days=365)
        self.assertEqual(len(report.feedbacks), 2)

    def test_unknown_requested_shop_is_kept_as_unknown_without_broadening(self):
        apis, factory = self.fixture()
        report = self.collect(shops='99')
        self.assertTrue(report.failed)
        self.assertEqual([shop['id'] for shop in report.shops], [99])
        factory.assert_not_called()
        self.assertFalse(analyze(report)['gaps'])

    def test_malformed_feedback_is_partial_and_other_shop_continues(self):
        apis, _ = self.fixture()
        apis[0].feedbacks.return_value = [{'id': 'bad', 'productDetails': None}]
        report = self.collect()
        self.assertTrue(report.failed)
        self.assertEqual(report.coverage[11]['feedbacks'], '部分')
        self.assertEqual(report.coverage[12]['feedbacks'], '完整')

    def test_vc_feedback_summary_includes_products_without_recent_orders(self):
        apis, _ = self.fixture()
        apis[0].feedbacks.return_value.append(feedback('no-order', nm=303, vc='BCS-NOOR-3'))
        report = self.collect()
        result = analyze(report)
        self.assertIn('BCS-NOOR-3', {row['key'] for row in result['vc_feedback']})
        self.assertNotIn('BCS-NOOR-3', {row['key'] for row in result['by_vc']})

    def test_all_statuses_feed_returns_but_only_valid_orders_with_vc_feed_sales(self):
        apis, _ = self.fixture()
        statuses = ('created', 'buyout', 'cancel', 'return', 'returnDefective', 'new-status')
        self.set_orders(apis[0], 11, [(order(status, status=status), VC) for status in statuses] +
                        [(order('unknown-vc', nm=909, status='buyout'), ''),
                         (order('bad-date', status='buyout', date='invalid'), VC)])
        report = self.collect()
        self.assertFalse(report.orders_complete)
        self.assertEqual(len(report.orders), 8)
        self.assertEqual(next(row for row in report.orders if row['order_id'] == 'unknown-vc')['vc'], '')
        result = analyze(report)
        returns = next(row for row in result['by_vc'] if row['key'] == VC)
        self.assertEqual((returns['orders'], returns['combined_returns'], returns['unknown']), (7, 4, 1))
        self.assertEqual(len(result['sales']), 1)
        sale = result['sales'][0]
        self.assertEqual((sale['orders7'], sale['orders14']), (2, 2))
        self.assertIn('部分', sale['sales_scope'])
        self.assertTrue(any('下单时间无效' in issue.message for issue in report.issues))

    def test_safe_protocol_reason_is_visible_in_report(self):
        apis, _ = self.fixture()
        apis[0].orders.side_effect = OpenAPIResponseError('订单响应缺少必要字段', status_code=400)
        report = self.collect()
        self.assertTrue(any('HTTP 400' in issue.message and '必要字段' in issue.message
                            for issue in report.issues))

    def test_xlsx_is_readable_and_untrusted_text_not_formula(self):
        self.fixture()
        report = self.collect()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.xlsx'
            write_report(report, target, details=True)
            wb = load_workbook(target)
            self.addCleanup(wb.close)
            self.assertIn('店铺链接缺口', wb.sheetnames)
            ws = wb['差评明细']
            self.assertEqual(ws.freeze_panes, 'A2')
            col = [cell.value for cell in ws[1]].index('评价正文') + 1
            self.assertEqual(ws.cell(2, col).value, '=1+1')
            self.assertEqual(ws.cell(2, col).data_type, 's')
            headers = [cell.value for cell in wb['VC退货汇总'][1]]
            rate_col = headers.index('兼容退货率') + 1
            self.assertEqual(wb['VC退货汇总'].cell(2, rate_col).number_format, '0.00%')
            self.assertFalse(list(target.parent.glob('.product-report-*')))

    def test_cli_exit_is_nonzero_when_report_partial(self):
        self.fixture(api_error=True)
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(shops='', order_days=30, feedback_days=0, wb_region='ru',
                                   out=str(Path(directory) / 'report.xlsx'))
            self.assertEqual(workflow.run(args), 1)
            self.assertTrue(Path(args.out).exists())

"""运营报告的离线协议、统计、完整性和工作簿测试；所有数据为合成夹具。"""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from openpyxl import load_workbook

from sellerops.core.errors import AuthenticationError, PlatformApiError, RateLimitError
from sellerops.domain.product_reports import ProductReport
from sellerops.features.catalog import product_report as workflow
from sellerops.features.catalog import product_report_data as data
from sellerops.features.catalog.product_report_analysis import analyze, return_metrics
from sellerops.features.catalog.product_report_xlsx import write_report
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


def register(vc=VC, nm=101, shop='店甲', date='2026-11-02', rid='r1'):
    return {'record_id': rid, 'fields': {'日期': date, 'BCS编号': vc, '店铺': [shop],
            '订单编号': rid, 'wb编号': str(nm), '商品中文名': '测试商品', '订单量': 2,
            '商品链接': {'text': '查看商品', 'link': data.product_url(nm)}}}


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
            report.orders.append({'shop_id': 11 if i == 0 else 12, 'shop_name': '甲' if i == 0 else '乙',
                                  'nm_id': '101' if i == 0 else '202', 'vc': VC, 'cn': '同一商品',
                                  'url': data.product_url(101 if i == 0 else 202),
                                  'status': 'cancel' if i == 0 else 'buyout'})
        report.feedbacks = [data.feedback_row(feedback(nm=101), 11, '甲', lambda *args: '同一商品')]
        result = analyze(report)
        self.assertAlmostEqual(result['by_cn'][0]['return_rate'], .1)
        self.assertEqual(len(result['by_vc']), 1)
        self.assertEqual({row['nm_id'] for row in result['links']}, {'101', '202'})
        self.assertEqual([row['negative'] for row in result['links']], [1, 0])
        self.assertEqual(result['vc_feedback'][0]['negative'], 1)

    def test_timezone_windows_link_conflicts_and_no_vc_tail_guess(self):
        start, end, feedback_start = data.windows(30, 0, NOW)
        self.assertEqual(start.date().isoformat(), '2026-10-04')
        self.assertEqual(end.date().isoformat(), '2026-11-03')
        self.assertIsNone(feedback_start)
        for days in (0, 32):
            with self.assertRaises(ValueError):
                data.windows(days, 0, NOW)
        raw = register()
        raw['fields']['日期'] = int(data.timestamp('2026-10-26T17:00:00Z').timestamp() * 1000)
        row = data.register_row(raw)
        self.assertEqual(row['日期'], '2026-10-27')
        self.assertEqual(row['url'], data.product_url(101))
        raw['fields']['wb编号'] = '999'
        self.assertIn('冲突', data.register_row(raw)['notes'])
        raw['fields'].pop('wb编号')
        raw['fields'].pop('商品链接')
        self.assertEqual(data.register_row(raw)['nm_id'], '')

    def test_only_unique_moscow_id_and_no_office_id(self):
        selected = data.moscow_warehouse([{'name': '成都仓', 'id': 9},
                                         {'name': '莫斯科仓库', 'id': 42, 'officeId': 999}], '莫斯科')
        self.assertEqual(selected['id'], 42)
        for rows in ([{'name': '成都仓', 'id': 9}],
                     [{'name': '莫斯科仓库', 'officeId': 999}],
                     [{'name': '莫斯科一', 'id': 1}, {'name': '莫斯科二', 'id': 2}]):
            with self.assertRaises(ValueError):
                data.moscow_warehouse(rows, '莫斯科')

    def test_feishu_markdown_links_preserve_original_and_extract_actual_url(self):
        for label in ('商品详情', data.product_url(101)):
            raw = register()
            raw['fields'].pop('wb编号')
            original = f'[{label}]({data.product_url(101)})'
            raw['fields']['商品链接'] = original
            row = data.register_row(raw)
            self.assertEqual(row['nm_id'], '101')
            self.assertEqual(row['url'], data.product_url(101))
            self.assertEqual(row['source_url'], original)
            self.assertEqual(row['notes'], '')
        raw = register()
        raw['fields']['商品链接'] = '[其他站点](https://example.invalid/catalog/101/)'
        self.assertIn('无法识别', data.register_row(raw)['notes'])
        raw['fields']['商品链接'] = f'[冲突]({data.product_url(202)})'
        self.assertIn('冲突', data.register_row(raw)['notes'])

    def test_snapshot_missing_wrong_shop_and_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            with patch.object(ProductSnapshotRepository, 'get_path', return_value=str(path)):
                self.assertIsNone(ProductSnapshotRepository.load_snapshot_document(11))
                for payload in ({'shopId': 12, 'rows': []}, {'shopId': 11, 'total': 2, 'rows': []}, {'total': 0}):
                    path.write_text(json.dumps(payload), encoding='utf8')
                    with self.assertRaises(ValueError):
                        ProductSnapshotRepository.load_snapshot_document(11)
                path.write_text('{broken', encoding='utf8')
                with self.assertRaises(Exception):
                    ProductSnapshotRepository.load_snapshot_document(11)


class TestReportWorkflow(unittest.TestCase):
    def fixture(self, *, missing=False, api_error=False, bad_warehouse=False, records=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        accesses = [OpenAPIShop(11, '店甲', 'secret-a'), OpenAPIShop(12, '店乙', 'secret-b')]
        stack.enter_context(patch.object(workflow.bcs, 'fetch_open_api_shops', return_value=accesses))
        stack.enter_context(patch.object(workflow, 'load_records', return_value=records or {}))
        stack.enter_context(patch.object(workflow.MappingRepository, 'build_vc_resolver',
                                         return_value=(lambda vc, fallback='': '测试商品', {})))
        def snapshot(sid):
            if missing and sid == 12:
                return None
            return {'shopId': sid, 'fetchedAt': '2026-11-01 12:00:00',
                    'rows': [{'nmId': 101 if sid == 11 else 202, 'vendorCode': VC}]}
        stack.enter_context(patch.object(workflow.ProductSnapshotRepository, 'load_snapshot_document', side_effect=snapshot))
        stack.enter_context(patch.object(workflow.tables, 'resolve_base', return_value='base'))
        stack.enter_context(patch.object(workflow.tables, 'resolve_table', return_value='table'))
        sources = [register(date='2026-10-27', rid='7day'), register(date='2026-10-26', rid='14day'),
                   register(vc='BCS-OTHR-9', nm=909, shop='外部店', date='2026-10-20', rid='external'),
                   register(vc='too-old', date='2026-10-19', rid='old')]
        stack.enter_context(patch.object(workflow.tables, 'list_records', return_value=sources))
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
        return workflow.collect(now=NOW, url='fixture-url', **kwargs)

    def test_collection_keeps_external_sources_and_windows(self):
        apis, factory = self.fixture()
        report = self.collect()
        self.assertFalse(report.failed)
        self.assertEqual(len(report.orders), 2)
        self.assertEqual(len(report.feedbacks), 4)
        self.assertEqual(report.coverage[11]['outside_warehouse'], 1)
        self.assertEqual(report.coverage[11]['outside_created'], 1)
        self.assertEqual(factory.call_args_list[0].args, ('secret-a', 'ru'))
        result = analyze(report)
        vc_sale = next(row for row in result['sales'] if row['vc'] == VC)
        self.assertEqual((vc_sale['orders7'], vc_sale['orders14']), (1, 2))
        self.assertIn('BCS-OTHR-9', {row['vc'] for row in result['sales']})
        self.assertNotIn('too-old', {row['vc'] for row in result['sales']})
        external = next(row for row in result['links'] if row['shop_name'] == '外部店')
        self.assertIsNone(external['negative'])
        self.assertIn('未知', external['note'])
        self.assertTrue(all(api.close.called for api in apis))

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
        self.fixture(missing=True, records={'BCS-OTHR-9': {'11': '2026-11-02 10:00:00'}})
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
        self.assertEqual({row['state'] for row in analyze(report)['gaps']}, {'缺少快照无法判断'})

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

    def test_feishu_failure_or_bad_date_not_empty_success(self):
        self.fixture()
        with patch.object(workflow.tables, 'list_records', side_effect=RuntimeError('secret-a')):
            report = self.collect()
        self.assertTrue(report.failed)
        self.assertFalse(report.feishu_complete)
        self.assertNotIn('secret-a', str(report.issues))
        with patch.object(workflow.tables, 'list_records', return_value=[register(date='invalid')]):
            report = self.collect()
        self.assertTrue(report.failed)

    def test_auxiliary_formula_rows_are_not_mistaken_for_undated_orders(self):
        self.fixture()
        with patch.object(workflow.tables, 'list_records', return_value=[register(),
                {'record_id': 'empty-row', '商品（链接）': '辅助公式展示值'}]):
            report = self.collect()
        self.assertFalse(report.failed)
        self.assertTrue(report.feishu_complete)
        self.assertEqual(len(report.sales), 1)
        self.assertTrue(any('占位行' in issue.message for issue in report.issues))
        with patch.object(workflow.tables, 'list_records', return_value=[
                {'record_id': 'real-undated', 'BCS编号': VC, '日期': ''}]):
            report = self.collect()
        self.assertTrue(report.failed)

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
            write_report(report, target)
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
                                   url='fixture', table='订单登记', out=str(Path(directory) / 'report.xlsx'))
            self.assertEqual(workflow.run(args), 1)
            self.assertTrue(Path(args.out).exists())

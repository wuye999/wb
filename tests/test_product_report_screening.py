"""默认筛选、同数字段缺口和完整VC单行展示的离线边界。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from sellerops.domain.product_reports import ProductReport, ReportCatalog
from sellerops.features.reports.product_report_data import order_row
from sellerops.features.reports.product_report_screening import product_key, screen_products
from sellerops.features.reports.product_report_xlsx import write_report
from sellerops.persistence.report_catalog import load_report_catalog
from sellerops.settings import paths
from test_product_report import order


VC = 'BCS-AAAA-123456789'
OTHER = 'BCS-BBBB-123456789-2'


class TestProductScreening(unittest.TestCase):
    def report(self):
        report = ProductReport('2026-11-03T16:00:00+08:00', '2026-10-05', '2026-11-03')
        report.shops = ([{'id': i, 'name': f'袁州{i}'} for i in range(1, 5)] +
                        [{'id': 9, 'name': '外部店'}, {'id': 10, 'name': '外部店'}])
        report.target_shop_ids = {1, 2, 3, 4}
        report.snapshots = {1: [{'vendorCode': VC, 'nmId': 101}],
                            2: [{'vendorCode': VC, 'nmId': 102}],
                            3: [{'vendorCode': OTHER, 'nmId': 103}], 4: []}
        report.coverage = {shop['id']: {'orders': '完整', 'feedbacks': '完整', 'snapshot': '可读取'} for shop in report.shops}
        report.catalog = ReportCatalog({'新商品'}, {OTHER: '新商品'})
        report.publish_records_complete = True
        return report

    def wb_order(self, vc=VC, nm=901, sid=9, date='2026-11-03', status='created'):
        raw = order(nm=nm, date=date, status=status)
        return order_row(raw, sid, '外部店', {str(nm): vc}, {'id': sid, 'name': '业务仓'}, lambda *args: '旧商品')

    def test_longest_digit_identity_rejects_ties_and_does_not_use_last_variant(self):
        self.assertEqual(product_key(OTHER), '123456789')
        self.assertEqual(product_key('BCS-A123-ozon-card-987654321-42'), '987654321')
        self.assertIsNone(product_key('BCS-111-222'))
        self.assertIsNone(product_key('BCS-NONE'))

    def test_weighted_score_merges_prefixes_but_each_local_vc_has_one_row(self):
        report = self.report()
        report.feedbacks = [{'vc': VC, 'stars': 1, 'cn': '', 'nm_id': '901', 'shop_name': '外部店'}] + [
            {'vc': OTHER, 'stars': 5, 'cn': '', 'nm_id': '902', 'shop_name': '外部店'} for _ in range(3)]
        self.assertFalse(screen_products(report).low_rating)  # 加权均分4，不能平均两个VC均分得到3。
        report.feedbacks = report.feedbacks[:2]
        result = screen_products(report)
        self.assertEqual(len(result.low_rating), 2)
        row = next(r for r in result.low_rating if r['vc'] == VC)
        self.assertEqual(row['present_shops'], '袁州1、袁州2')
        self.assertEqual(row['rating'], 3)

    def test_return_denominator_and_risk_exclusion_use_all_accounts_and_prefixes(self):
        report = self.report()
        report.orders = [self.wb_order()] + [
            self.wb_order(vc=OTHER, status=status, nm=999)
            for status in ['cancel'] * 3 + ['buyout'] * 7 + ['created'] * 10]
        result = screen_products(report)
        self.assertEqual(len(result.high_returns), 2)
        self.assertEqual(result.high_returns[0]['completed_compatible_return_rate'], 0.3)
        self.assertEqual(result.high_returns[0]['completed'], 10)
        self.assertFalse(result.recent)

    def test_recent_one_row_per_full_vc_retains_nm_pairs_and_normalized_missing_shops(self):
        report = self.report()
        report.orders = [self.wb_order(), self.wb_order(nm=902, sid=10), self.wb_order(date='2026-10-21')]
        rows = screen_products(report).recent
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['orders7'], row['orders14']), (2, 3))
        self.assertEqual(row['cn'], '新商品')  # 当前映射优先于旧报告中文名。
        self.assertEqual(row['present_shops'], '袁州1、袁州2、袁州3')
        self.assertEqual(row['missing_shops'], '袁州4')
        self.assertIn('外部店(9)：901', row['source_nms'])
        self.assertIn('外部店(10)：902', row['source_nms'])
        self.assertIn('评分未知', row['note'])
        self.assertIn('退货率未知', row['note'])

    def test_unknown_pending_and_trash_are_not_reported_as_missing(self):
        for state in ('unknown', 'partial', 'pending', 'trash'):
            with self.subTest(state=state):
                report = self.report()
                report.orders = [self.wb_order()]
                if state == 'unknown':
                    report.snapshots[4] = None
                elif state == 'partial':
                    report.coverage[4]['snapshot'] = '部分'
                elif state == 'pending':
                    report.publish_records = {OTHER: {'4': 'accepted'}}
                else:
                    report.snapshots[4] = [{'vendorCode': OTHER, 'nmId': 104, 'trashedAt': 'date'}]
                row = screen_products(report).recent[0]
                self.assertEqual(row['missing_shops'], '')
                self.assertIn('袁州4', row['waiting_shops'])

    def test_current_name_must_match_price_book_and_ambiguity_is_not_guessed(self):
        report = self.report()
        report.orders = [self.wb_order()]
        report.catalog.mapped_names = {VC: '不在价格表'}
        self.assertFalse(screen_products(report).recent)
        report.catalog.price_names.add('另一个商品')
        report.catalog.mapped_names = {VC: '新商品', OTHER: '另一个商品'}
        self.assertFalse(screen_products(report).recent)

    def test_catalog_only_names_and_out_of_scope_submissions_do_not_create_issues(self):
        report = self.report()
        report.orders = [self.wb_order()]
        report.catalog.price_names.add('示例目录商品')
        report.catalog.mapped_names.update({'TEST-HISTORY': '示例历史商品', 'TEST-421-248': '示例历史商品'})
        report.publish_records = {'TEST-HISTORY': {}, 'TEST-421-248': {'9': 'accepted', '4': ''}}
        report.snapshots[9] = [{'vendorCode': 'TEST-HISTORY', 'nmId': 901}]

        result = screen_products(report)
        self.assertEqual(result.issues, [])
        self.assertEqual([row['vc'] for row in result.recent], [VC])
        self.assertEqual(result.recent[0]['missing_shops'], report.shops[3]['name'])

    def test_invalid_codes_from_current_evidence_still_fail_screening(self):
        for source in ('order', 'feedback', 'snapshot', 'submission'):
            for vc in ('TEST-WITHOUT-DIGITS', 'TEST-421-248'):
                with self.subTest(source=source, vc=vc):
                    report = self.report()
                    report.orders = [self.wb_order()]
                    if source == 'order':
                        report.orders.append(self.wb_order(vc=vc, nm=902))
                    elif source == 'feedback':
                        report.feedbacks = [{'vc': vc, 'stars': 5}]
                    elif source == 'snapshot':
                        report.snapshots[4] = [{'vendorCode': vc, 'nmId': 104}]
                    else:
                        report.publish_records = {vc: {'4': 'accepted'}}
                    result = screen_products(report)
                    self.assertTrue(result.failed)
                    issue, = result.issues
                    self.assertEqual((issue.source, issue.severity), ('默认筛选', '错误'))
                    self.assertIn('1个VC没有唯一最长数字段', issue.message)
                    if source == 'snapshot':
                        self.assertEqual(result.recent[0]['missing_shops'], '')
                        self.assertIn('快照未知', result.recent[0]['waiting_shops'])

    def test_rating_threshold_inclusive_and_missing_catalog_is_visible(self):
        report = self.report()
        report.feedbacks = [{'vc': VC, 'stars': star, 'cn': '', 'nm_id': '', 'shop_name': '外部店'} for star in [3, 4]]
        report.orders = [self.wb_order()]
        result = screen_products(report)
        self.assertEqual(result.low_rating[0]['rating'], 3.5)
        self.assertFalse(result.recent)
        report.catalog = None
        self.assertTrue(screen_products(report).failed)

    def test_partial_order_coverage_labels_known_counts_for_each_full_vc(self):
        report = self.report()
        report.orders = [self.wb_order(), self.wb_order(vc=OTHER, nm=902)]
        report.coverage[10]['orders'] = '未知'
        rows = screen_products(report).recent
        self.assertEqual({row['vc']: row['orders14'] for row in rows}, {VC: 1, OTHER: 1})
        for row in rows:
            self.assertIn('出单件数仅为已知部分', row['note'])

    def test_default_workbook_is_compact_without_product_urls_and_preserves_unknowns(self):
        report = self.report()
        report.orders = [self.wb_order()]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.xlsx'
            write_report(report, output)
            wb = load_workbook(output)
            self.addCleanup(wb.close)
            self.assertEqual(wb.sheetnames[:3], ['低评分VC', '高退货率VC', '近期出单筛选'])
            self.assertNotIn('店铺链接指标', wb.sheetnames)
            ws = wb['近期出单筛选']
            self.assertEqual(ws.max_row, 2)
            headers = [cell.value for cell in ws[1]]
            self.assertIn('缺少店铺', headers)
            self.assertFalse(any('链接' in str(cell.value) for sheet in wb for cell in sheet[1]))
            self.assertEqual(ws.cell(2, headers.index('缺少店铺') + 1).value, '袁州4')


class TestReportCatalog(unittest.TestCase):
    def test_reads_name_and_vc_with_missing_optional_price_and_never_reads_export(self):
        from catalog_fixture import map_vcs, write_book
        self.addCleanup(paths.configure, paths.current.data_dir)
        with tempfile.TemporaryDirectory() as directory:
            paths.configure(directory)
            write_book([('S1', '\u65b0\u5546\u54c1')], ('\u5356\u5bb6SKU', '\u4ea7\u54c1\u4e2d\u6587\u540d'))
            map_vcs({OTHER: 'S1'})
            price = Path(paths.current.price_book_xlsx)
            before = price.read_bytes()
            Path(paths.current.mapping_xlsx).write_bytes(b'not a workbook')
            result = load_report_catalog()
            self.assertEqual(result.price_names, {'\u65b0\u5546\u54c1'})
            self.assertEqual(result.mapped_names, {OTHER: '\u65b0\u5546\u54c1'})
            self.assertEqual(price.read_bytes(), before)

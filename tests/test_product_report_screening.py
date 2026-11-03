"""默认筛选、同数字段缺口和完整VC单行展示的离线边界。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from sellerops.domain.product_reports import ProductReport, ReportCatalog
from sellerops.features.catalog.product_report_screening import product_key, screen_products
from sellerops.features.catalog.product_report_xlsx import write_report
from sellerops.persistence.report_catalog import load_report_catalog
from sellerops.settings import paths


VC = 'BCS-AAAA-123456789'
OTHER = 'BCS-BBBB-123456789-2'


class TestProductScreening(unittest.TestCase):
    def report(self):
        report = ProductReport('2026-11-03T16:00:00+08:00', '2026-10-05', '2026-11-03')
        report.shops = [{'id': i, 'name': f'袁州{i}'} for i in range(1, 5)] + [{'id': 9, 'name': '外部店'}]
        report.target_shop_ids = {1, 2, 3, 4}
        report.snapshots = {1: [{'vendorCode': VC, 'nmId': 101}],
                            2: [{'vendorCode': VC, 'nmId': 102}],
                            3: [{'vendorCode': OTHER, 'nmId': 103}], 4: []}
        report.coverage = {shop['id']: {'orders': '完整', 'feedbacks': '完整', 'snapshot': '可读取'} for shop in report.shops}
        report.catalog = ReportCatalog({'新商品'}, {OTHER: '新商品'})
        report.feishu_complete = report.publish_records_complete = True
        return report

    def sales(self, vc=VC, nm='901', shop='外部店', date='2026-11-03'):
        return {'vc': vc, 'nm_id': nm, 'shops': [shop], 'cn': '旧商品', '日期': date, 'qty': 1}

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
        report.sales = [self.sales()]
        report.orders = [{'vc': OTHER, 'status': status, 'cn': '', 'nm_id': '999', 'shop_name': '外部店'}
                         for status in ['cancel'] * 3 + ['buyout'] * 7 + ['created'] * 10]
        result = screen_products(report)
        self.assertEqual(len(result.high_returns), 2)
        self.assertEqual(result.high_returns[0]['done_rate'], 0.3)
        self.assertEqual(result.high_returns[0]['completed'], 10)
        self.assertFalse(result.recent)

    def test_recent_one_row_per_full_vc_retains_nm_pairs_and_normalized_missing_shops(self):
        report = self.report()
        report.sales = [self.sales(), self.sales(nm='902', shop='另一个外部店'), self.sales(date='2026-10-21')]
        row = screen_products(report).recent[0]
        self.assertEqual((row['orders7'], row['orders14']), (2, 3))
        self.assertEqual(row['cn'], '新商品')  # 当前映射优先于旧报告中文名。
        self.assertEqual(row['present_shops'], '袁州1、袁州2、袁州3')
        self.assertEqual(row['missing_shops'], '袁州4')
        self.assertIn('外部店：901', row['source_nms'])
        self.assertIn('另一个外部店：902', row['source_nms'])
        self.assertIn('评分未知', row['note'])
        self.assertIn('退货率未知', row['note'])

    def test_unknown_pending_and_trash_are_not_reported_as_missing(self):
        for state in ('unknown', 'partial', 'pending', 'trash'):
            with self.subTest(state=state):
                report = self.report()
                report.sales = [self.sales()]
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
        report.sales = [self.sales()]
        report.catalog.mapped_names = {VC: '不在价格表'}
        self.assertFalse(screen_products(report).recent)
        report.catalog.price_names.add('另一个商品')
        report.catalog.mapped_names = {VC: '新商品', OTHER: '另一个商品'}
        self.assertFalse(screen_products(report).recent)

    def test_rating_threshold_inclusive_and_missing_catalog_is_visible(self):
        report = self.report()
        report.feedbacks = [{'vc': VC, 'stars': star, 'cn': '', 'nm_id': '', 'shop_name': '外部店'} for star in [3, 4]]
        self.assertEqual(screen_products(report).low_rating[0]['rating'], 3.5)
        report.catalog = None
        self.assertTrue(screen_products(report).failed)

    def test_missing_nm_is_only_resolved_from_unique_same_shop_identity(self):
        report = self.report()
        report.sales = [self.sales(nm='', shop='袁州3')]
        self.assertEqual(screen_products(report).recent[0]['source_nms'], '袁州3：103')
        report.sales = [self.sales(nm='', shop='不存在的店铺')]
        self.assertIn('nmID未知', screen_products(report).recent[0]['source_nms'])
        report.sales = [self.sales(nm='', shop='外部店'), self.sales(nm='777', shop='外部店')]
        self.assertEqual(screen_products(report).recent[0]['source_nms'], '外部店：777')

    def test_default_workbook_is_compact_without_product_urls_and_preserves_unknowns(self):
        report = self.report()
        report.sales = [self.sales()]
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
    def test_only_name_and_vc_headers_are_needed_and_inputs_are_not_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            price, mapping = Path(directory) / 'price.xlsx', Path(directory) / 'mapping.xlsx'
            for target, sheet, values in [(price, 'Sheet1', [['无关列', '产品中文名'], [0, '新商品']]),
                                          (mapping, '映射总表', [['vendorCode', '产品中文名'], [OTHER, '新商品']])]:
                wb = Workbook()
                wb.active.title = sheet
                for row in values:
                    wb.active.append(row)
                wb.save(target)
                wb.close()
            before = price.read_bytes(), mapping.read_bytes()
            with patch.object(paths, 'PRICE_BOOK_XLSX', str(price)), patch.object(paths, 'MAPPING_XLSX', str(mapping)):
                result = load_report_catalog()
            self.assertEqual(result.price_names, {'新商品'})
            self.assertEqual(result.mapped_names, {OTHER: '新商品'})
            self.assertEqual(before, (price.read_bytes(), mapping.read_bytes()))

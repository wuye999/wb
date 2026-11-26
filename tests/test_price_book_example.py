"""Offline contract check for the example price book delivered with SellerOps."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from sellerops.features.publishing.publish_inputs import parse_dims_str
from sellerops.persistence.price_book import (
    BOSS_COLUMNS, SHIPPING_MODE_MABANG, SHIPPING_MODE_WB, inspect_price_book, read_price_book,
)


class PriceBookExampleTest(TestCase):
    def test_example_matches_the_production_price_book_contract(self):
        path = Path(__file__).resolve().parents[1] / 'examples' / '商品价格表.example.xlsx'
        book = load_workbook(path, read_only=True)
        try:
            self.assertEqual(book.sheetnames, ['Sheet1'])
            headers = next(book['Sheet1'].values)
            self.assertEqual(headers, tuple(BOSS_COLUMNS.values()))
        finally:
            book.close()

        mabang, wb = read_price_book(str(path))
        self.assertEqual((mabang['shipping_mode'], wb['shipping_mode']), (SHIPPING_MODE_MABANG, SHIPPING_MODE_WB))
        self.assertEqual(mabang['sku'], mabang['seller_sku'])
        self.assertTrue(mabang['seller_sku'])
        self.assertTrue(mabang['stock_sku'])
        self.assertEqual(wb['stock_sku'], '')
        self.assertEqual(wb['seller_sku'], '')
        self.assertTrue(wb['name_unique'])
        self.assertEqual(wb['sku'], wb['cn'])
        for row in (mabang, wb):
            self.assertGreater(row['minimum_price'], 0)
            self.assertRegex(row['prefix'], r'^[A-Z]{4}$')
            self.assertTrue(all(value > 0 for value in parse_dims_str(row['dimensions'])))

        health = inspect_price_book(path)
        self.assertEqual(health['status'], 'ready')
        self.assertEqual(health['data_rows'], 2)
        for key in ('missing_headers', 'duplicate_headers', 'duplicate_optional_headers', 'issue_counts', 'skipped_checks'):
            self.assertFalse(health[key], key)

    def test_row_diagnostics_preserve_optional_values_and_identify_prefix_conflicts(self):
        path = Path(self.enterContext(TemporaryDirectory())) / 'contract.xlsx'
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Sheet1'
        sheet.append(('卖家SKU', '产品中文名', '尺寸', '最低售价', 'vendorCode前缀码', '发货方式', '马帮库存SKU'))
        for row in (
            ('A', '甲', '无效尺寸', 10, ' tEsT ', ' 不处理 ', 'stock'),
            ('', '唯一中文名', '1.2 x 2 X 3 / .0001', 10, 'TEST', '不处理', None),
            ('B', '乙', '2x3*4', 10, 'ſEST', 'WB发货', None),
            ('C', '丙', '0*2*3/1', 10, 'AB12', 'WB发货', None),
            ('D', '丁', '1*2*3/0', 10, 0, 'WB发货', None),
            ('E', '戊', None, 10, '   ', 'WB发货', None),
            (None, None, None, 10, 'TEST', 'WB发货', None),
        ):
            sheet.append(row)
        workbook.save(path)
        workbook.close()
        original = path.read_bytes()
        health = inspect_price_book(path)
        self.assertEqual(health['status'], 'ready')
        self.assertEqual(health['issue_counts'], {
            'invalid_dimensions': 3, 'invalid_prefix': 3, 'duplicate_prefix': 2,
            'skip_shipping_with_mabang_sku': 1, 'missing_product_identity': 1,
        })
        self.assertEqual(health['row_samples']['duplicate_prefix'], [2, 3])
        self.assertEqual(health['row_samples']['invalid_dimensions'], [2, 5, 6])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(parse_dims_str('1.2 x 2 X 3 / .0001'), (2, 2, 3, 0.0))
        self.assertEqual(parse_dims_str('2x3*4'), (2, 3, 4, 0.0))

    def test_price_and_dimension_formulas_share_one_lazy_cache(self):
        path = Path(self.enterContext(TemporaryDirectory())) / 'formulas.xlsx'
        headers = ('产品中文名', '最低售价', '尺寸')
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Sheet1'
        sheet.append(headers)
        sheet.append(('甲', 10, '="2x3x4"'))
        sheet.append((None, None, None))
        sheet.append(('乙', '=2+3', '="bad"'))
        sheet.append(('丙', '=5+6', '="1*2*3/0.5"'))
        workbook.save(path)
        workbook.close()

        cached = Workbook()
        sheet = cached.active
        sheet.title = 'Sheet1'
        sheet.append(headers)
        sheet.append(('甲', 10, '2x3x4'))
        sheet.append((None, None, None))
        sheet.append(('乙', 5, 'bad'))
        sheet.append(('丙', None, None))
        with patch('openpyxl.load_workbook', side_effect=[load_workbook(path, read_only=True), cached]) as load:
            health = inspect_price_book(path)
        self.assertEqual([call.kwargs.get('data_only', False) for call in load.call_args_list], [False, True])
        self.assertEqual(health['row_samples'], {
            'invalid_dimensions': [4], 'price_formula_without_cache': [5],
            'dimensions_formula_without_cache': [5],
        })

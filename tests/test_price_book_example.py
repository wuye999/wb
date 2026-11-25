"""Offline contract check for the example price book delivered with SellerOps."""
from pathlib import Path
from unittest import TestCase

from openpyxl import load_workbook

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

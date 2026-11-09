"""BCS 重复行只保留完整原行；冲突不覆盖快照或单店表，全部离线。"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from openpyxl import Workbook, load_workbook

from sellerops.core.errors import PlatformApiError
from sellerops.core.files import atomic_dump_json
from sellerops.domain.bcs_products import deduplicate_bcs_products
from sellerops.features.catalog import mapping, products, single_shop
from sellerops.features.pricing import discount_bcs
from sellerops.integrations.bcs.client import BCSClient
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.settings import paths


VC = 'BCS-TEST-100'


def complete_row(**overrides):
    return {'shopId': 1, 'id': 10, 'nmId': 101, 'vendorCode': VC,
            'title': '测试商品', 'currencyIsoCode': 'CNY', 'discount': 49,
            'clubDiscount': 0, 'trashedAt': None, 'updateTime': 2000,
            'sizeList': [{'chrtId': 11, 'price': 10,
                          'stockList': [{'warehouseId': 42, 'amount': 0}]}], **overrides}


def incomplete_row():
    return complete_row(id=20, updateTime=1000, currencyIsoCode=None,
                        discount=None, clubDiscount=None)


class TestBCSDeduplication(unittest.TestCase):
    def test_complete_original_row_wins_in_either_order_and_input_is_unchanged(self):
        full, empty = complete_row(), incomplete_row()
        for rows in ([full, empty], [empty, full], [empty, full, empty]):
            before = deepcopy(rows)
            result = deduplicate_bcs_products(rows, 1)
            self.assertEqual(result, [full])
            self.assertIs(result[0], full)
            self.assertEqual(rows, before)

    def test_equivalent_complete_rows_use_metadata_only_after_business_equality(self):
        newer = complete_row(id=9, updateTime=3000)
        for rows in ([complete_row(), newer], [newer, complete_row()]):
            self.assertEqual(deduplicate_bcs_products(rows, 1), [newer])
        # Real zero and False are populated values, not reasons to choose a null row.
        zero = complete_row(discount=0, editableSizePrice=False)
        empty = complete_row(discount=None, editableSizePrice=None)
        self.assertEqual(deduplicate_bcs_products([empty, zero], 1), [zero])

    def test_conflicting_identity_values_status_or_specs_are_rejected(self):
        variants = [complete_row(nmId=102), complete_row(vendorCode='OTHER'),
                    complete_row(discount=50, updateTime=9000),
                    complete_row(trashedAt='2026-10-01'),
                    complete_row(sizeList=[{'chrtId': 12}]),
                    complete_row(shopId=2), complete_row(nmId=None),
                    complete_row(vendorCode=None)]
        for other in variants:
            with self.subTest(other=other), self.assertRaises(ValueError):
                deduplicate_bcs_products([complete_row(), other], 1)
        with self.assertRaises(ValueError):
            deduplicate_bcs_products([complete_row(discount=0), complete_row(discount=False)], 1)

    def test_complementary_incomplete_rows_are_not_spliced_together(self):
        with self.assertRaisesRegex(ValueError, '完整原行'):
            deduplicate_bcs_products([complete_row(discount=None),
                                      complete_row(currencyIsoCode=None)], 1)

    def test_unique_incomplete_products_remain_unchanged_and_order_is_stable(self):
        unknown = {'vendorCode': 'UNKNOWN', 'nmId': None, 'sizeList': []}
        other = complete_row(nmId=102, vendorCode='OTHER', sizeList=[])
        self.assertEqual(deduplicate_bcs_products([unknown, incomplete_row(), other, complete_row()], 1),
                         [unknown, complete_row(), other])
        self.assertEqual(deduplicate_bcs_products([], 1), [])
        with self.assertRaises(ValueError):
            deduplicate_bcs_products([unknown, unknown], 1)


class TestBCSDuplicateWorkflow(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        previous = paths.DATA_DIR
        self.addCleanup(paths.configure, previous)
        self.data = Path(folder.name)
        paths.configure(self.data)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.output = io.StringIO()
        stack.enter_context(redirect_stdout(self.output))
        stack.enter_context(redirect_stderr(self.output))
        stack.enter_context(patch.object(BCSClient, 'base_url', new_callable=PropertyMock,
                                        return_value='https://example.invalid'))
        stack.enter_context(patch.object(mapping, '_shop_id_cache', 1))

    def run_cli(self, *args):
        from sellerops.cli.main import main
        return main(['--data-dir', str(self.data), *args])

    def test_original_response_count_is_checked_before_deduplication(self):
        raw = [complete_row(), incomplete_row()]
        client = BCSClient()
        with patch.object(client, 'get', return_value={'code': 200, 'rows': raw, 'total': 2}):
            self.assertEqual(client.fetch_shop_products(1), [complete_row()])
        with patch.object(client, 'get', return_value={'code': 200, 'rows': raw, 'total': 1}), \
                self.assertRaises(PlatformApiError):
            client.fetch_shop_products(1)

    def test_fetch_persists_unique_rows_and_merge_preserves_full_values(self):
        raw = [incomplete_row(), complete_row()]
        with patch.object(products.bcs.client, 'get', return_value={'code': 200, 'rows': raw, 'total': 2}), \
                patch.object(products.bcs, 'sync_shop') as sync:
            self.assertEqual(self.run_cli('fetch', '--shop-id', '1', '--no-sync'), 0)
            sync.assert_not_called()
        document = ProductSnapshotRepository.load_snapshot_document(1)
        self.assertEqual(document['rows'], [complete_row()])
        self.assertEqual((document['total'], document['fetchedCount']), (1, 1))
        self.assertEqual(document['warnings'], {'missingSpecProducts': 0,
                                               'missingStockProducts': 0, 'missingPriceProducts': 0})
        self.assertEqual(len(ProductSnapshotRepository.load_shop_rows(1)), 1)
        self.assertIn('移除1条重复记录', self.output.getvalue())
        book = Workbook()
        book.active.title = 'Sheet1'
        book.active.append(['卖家SKU', '产品中文名', '实际售价', 'vendorCode前缀码'])
        book.active.append(['SKU1', '商品', 10, 'TEST'])
        book.save(paths.PRICE_BOOK_XLSX)
        book.close()
        atomic_dump_json(paths.STATUS_JSON, {'shops': [{'id': 1, 'name': '店1'}]})
        self.assertEqual(self.run_cli('merge'), 0)
        self.assert_single_values(1)
        book = load_workbook(paths.MAPPING_XLSX, read_only=True, data_only=True)
        try:
            values = list(book['映射总表'].iter_rows(values_only=True))
            self.assertEqual(len(values), 2)
            self.assertEqual(values[1][values[0].index('折扣%')], 49)
        finally:
            book.close()

    def assert_single_values(self, expected_count):
        book = load_workbook(paths.shop_mapping_xlsx(1, '店1'), read_only=True, data_only=True)
        try:
            values = list(book['在架映射明细'].iter_rows(values_only=True))
            self.assertEqual(len(values) - 1, expected_count)
            self.assertEqual(values[1][values[0].index('折扣%')], 49)
            self.assertEqual(values[1][values[0].index('club折扣%')], 0)
            self.assertEqual(values[1][values[0].index('本店库存')], '0')
        finally:
            book.close()

    def test_conflicting_fetch_preserves_existing_snapshot(self):
        path = Path(paths.shop_json_path(1))
        atomic_dump_json(path, {'shopId': 1, 'total': 1, 'rows': [complete_row()]})
        before = path.read_bytes()
        raw = [complete_row(), complete_row(discount=50)]
        with patch.object(products.bcs.client, 'get', return_value={'code': 200, 'rows': raw, 'total': 2}):
            self.assertEqual(self.run_cli('fetch', '--shop-id', '1', '--no-sync'), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_old_snapshot_single_table_is_unique_and_conflict_cannot_overwrite(self):
        raw = [incomplete_row(), complete_row()]
        options = {'registry': ({VC: {'cn': '商品'}}, {}, {}), 'boss': [], 'prefix_map': {}}
        self.assertEqual(single_shop.build_single_shop_xlsx(1, '店1', rows=raw, **options), (1, 0))
        self.assert_single_values(1)
        path = Path(paths.shop_mapping_xlsx(1, '店1'))
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            single_shop.build_single_shop_xlsx(1, '店1',
                rows=[complete_row(), complete_row(discount=50)], **options)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(single_shop.build_single_shop_xlsx(1, 'unmapped', rows=raw,
            registry=({}, {}, {}), boss=[], prefix_map={}), (0, 1))

    def test_direct_bcs_discount_also_collects_each_product_once(self):
        raw = [complete_row(), complete_row(id=20)]
        with patch.object(discount_bcs.bcs.client, 'get', return_value={'code': 200, 'rows': raw, 'total': 2}):
            items, _ = discount_bcs.collect(1, 40, 0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['nmID'], 101)


if __name__ == '__main__':
    unittest.main()

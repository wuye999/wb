"""Real local files and offline platform responses for fetch/merge correctness."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from sellerops.core.files import atomic_dump_json
from sellerops.settings import paths


class TestCatalogWorkflow(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = paths.DATA_DIR
        self.addCleanup(paths.configure, previous)
        self.data = Path(directory.name)
        paths.configure(self.data)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        from sellerops.features.catalog import mapping, single_shop
        self.stack.enter_context(patch.object(mapping, '_shop_id_cache', 1))
        self.shops = [{'id': 1, 'name': 'live'}, {'id': 2, 'name': 'empty'}]
        self.live_vc = 'BCS-TEST-123'
        self.stale_vc = 'BCS-OLDX-456'
        self.live = {'nmId': 123, 'vendorCode': self.live_vc, 'title': 'live',
                     'sizeList': [{'chrtId': 11, 'price': 12, 'stockList': []}]}
        self.stale = {'nmId': 456, 'vendorCode': self.stale_vc, 'title': 'stale',
                      'sizeList': [{'chrtId': 22, 'price': 24, 'stockList': []}]}
        book = Workbook()
        book.active.title = 'Sheet1'
        book.active.append(['卖家SKU', '产品中文名', '实际售价', 'vendorCode前缀码'])
        book.active.append(['SKU1', '当前商品', 10, 'TEST'])
        book.active.append(['SKU2', '历史商品', 20, 'OLDX'])
        book.save(paths.PRICE_BOOK_XLSX)
        book.close()
        atomic_dump_json(paths.VC_KNOWN_JSON, {
            self.live_vc: {'cn': '当前商品', 'sku': 'SKU1', 'dp': 10},
            self.stale_vc: {'cn': '历史商品', 'sku': 'SKU2', 'dp': 20},
        })
        atomic_dump_json(paths.VC_OVERRIDE_JSON, {})
        atomic_dump_json(paths.VC_EXCLUDED_JSON, {})
        self.save_snapshot(1, [self.live])
        self.save_snapshot(2, [])
        atomic_dump_json(paths.STATUS_JSON, {'shops': self.shops, 'errors': []})
        # The old local table still contains the item that has since left shop 2.
        single_shop.build_single_shop_xlsx(1, 'live', rows=[self.live])
        single_shop.build_single_shop_xlsx(2, 'empty', rows=[self.stale])

    def save_snapshot(self, sid, rows):
        atomic_dump_json(paths.shop_json_path(sid), {'shopId': sid, 'total': len(rows), 'rows': rows})

    def run_cli(self, *argv):
        from sellerops.cli.main import main
        return main(['--data-dir', str(self.data), *argv])

    def mapping_vendor_codes(self):
        book = load_workbook(paths.MAPPING_XLSX, read_only=True, data_only=True)
        try:
            sheet = book['映射总表']
            headers = [cell.value for cell in sheet[1]]
            column = headers.index('vendorCode')
            return {row[column] for row in sheet.iter_rows(min_row=2, values_only=True) if row[column]}
        finally:
            book.close()

    def assert_shop_empty(self, sid, name):
        from sellerops.features.catalog.single_shop import load_single_shop_rows
        mapped, unmapped = load_single_shop_rows(sid, name)
        self.assertEqual(mapped, {})
        self.assertEqual(unmapped, [])

    def test_merge_removes_empty_shop_rows_from_local_table_and_total(self):
        self.assertEqual(self.run_cli('merge'), 0)
        self.assert_shop_empty(2, 'empty')
        self.assertEqual(self.mapping_vendor_codes(), {self.live_vc})
        # Removing an inactive item from coverage must preserve historical ownership.
        registry = json.loads(Path(paths.VC_KNOWN_JSON).read_text(encoding='utf8'))
        self.assertIn(self.stale_vc, registry)

    def test_merge_all_empty_shops_rebuilds_valid_empty_workbooks(self):
        self.save_snapshot(1, [])
        self.assertEqual(self.run_cli('merge'), 0)
        self.assert_shop_empty(1, 'live')
        self.assert_shop_empty(2, 'empty')
        self.assertEqual(self.mapping_vendor_codes(), set())

    def test_missing_snapshot_fails_before_refreshing_any_shop_table(self):
        first = Path(paths.shop_mapping_xlsx(1, 'live'))
        second = Path(paths.shop_mapping_xlsx(2, 'empty'))
        before = (first.read_bytes(), second.read_bytes())
        Path(paths.shop_json_path(2)).unlink()
        self.assertEqual(self.run_cli('merge'), 1)
        self.assertEqual((first.read_bytes(), second.read_bytes()), before)
        self.assertFalse(Path(paths.MAPPING_XLSX).exists())

    def test_corrupt_snapshot_is_not_treated_as_empty_or_silently_merged(self):
        first = Path(paths.shop_mapping_xlsx(1, 'live'))
        second = Path(paths.shop_mapping_xlsx(2, 'empty'))
        before = (first.read_bytes(), second.read_bytes())
        Path(paths.shop_json_path(2)).write_text('{broken', encoding='utf8')
        self.assertEqual(self.run_cli('merge'), 1)
        self.assertEqual((first.read_bytes(), second.read_bytes()), before)
        self.assertFalse(Path(paths.MAPPING_XLSX).exists())

    def test_missing_snapshot_of_archived_shop_does_not_block_live_merge(self):
        archive = Path(paths.SHOPS_ARCHIVE_DIR)
        archive.mkdir(parents=True)
        Path(paths.shop_mapping_xlsx(2, 'empty')).rename(archive / 'shop_2_empty.xlsx')
        Path(paths.shop_json_path(2)).unlink()
        self.assertEqual(self.run_cli('merge'), 0)
        self.assertEqual(self.mapping_vendor_codes(), {self.live_vc})

    def test_partial_fetch_failure_returns_nonzero_saves_errors_and_keeps_old_snapshot(self):
        from sellerops.integrations.bcs import client as bcs
        from sellerops.features.catalog import products
        self.save_snapshot(2, [self.stale])
        old_failed = Path(paths.shop_json_path(2)).read_bytes()
        shops = self.shops + [{'id': 3, 'name': 'after_failure'}]
        visited = []

        def fetch(sid, filter_type):
            visited.append(sid)
            self.assertEqual(filter_type, 'BASE')
            if sid == 2:
                raise RuntimeError('fixture read failed')
            return [self.live] if sid == 1 else []

        with patch.object(bcs, 'fetch_shop_list', return_value=shops), \
                patch.object(bcs, 'fetch_shop_products', side_effect=fetch), \
                patch.object(bcs, 'sync_shops_parallel') as sync, \
                patch.object(bcs, 'sync_shop') as sync_one, patch.object(products.time, 'sleep'):
            self.assertEqual(self.run_cli('fetch', '--no-sync'), 1)
        self.assertEqual(visited, [1, 2, 3])
        self.assertEqual(Path(paths.shop_json_path(2)).read_bytes(), old_failed)
        self.assertEqual(json.loads(Path(paths.shop_json_path(1)).read_text(encoding='utf8'))['rows'], [self.live])
        self.assertEqual(json.loads(Path(paths.shop_json_path(3)).read_text(encoding='utf8'))['rows'], [])
        status = json.loads(Path(paths.STATUS_JSON).read_text(encoding='utf8'))
        self.assertEqual([error['id'] for error in status['errors']], [2])
        sync.assert_not_called()
        sync_one.assert_not_called()

    def test_successful_fetch_including_empty_shop_returns_zero_and_clears_old_errors(self):
        from sellerops.integrations.bcs import client as bcs
        from sellerops.features.catalog import products
        atomic_dump_json(paths.STATUS_JSON, {'shops': self.shops, 'errors': [{'id': 2, 'error': 'previous failure'}]})
        with patch.object(bcs, 'fetch_shop_list', return_value=self.shops), \
                patch.object(bcs, 'fetch_shop_products', side_effect=lambda sid, filter_type: [self.live] if sid == 1 else []), \
                patch.object(bcs, 'sync_shops_parallel') as sync, \
                patch.object(bcs, 'sync_shop') as sync_one, patch.object(products.time, 'sleep'):
            self.assertEqual(self.run_cli('fetch', '--no-sync'), 0)
        status = json.loads(Path(paths.STATUS_JSON).read_text(encoding='utf8'))
        self.assertEqual(status['errors'], [])
        self.assertEqual(json.loads(Path(paths.shop_json_path(2)).read_text(encoding='utf8'))['rows'], [])
        sync.assert_not_called()
        sync_one.assert_not_called()

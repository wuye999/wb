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


def exit_after_first_shop_save(directory):
    """退出整个子进程，留下操作系统释放的锁和未经 finally 清理的现场。"""
    import os
    from sellerops.cli.main import main
    work = directory / 'state/.mapping-work'
    replace = os.replace

    def exit_during_commit(src, dst):
        replace(src, dst)
        if Path(src).parent == work:
            os._exit(23)

    with patch.object(os, 'replace', side_effect=exit_during_commit), redirect_stdout(io.StringIO()):
        main(['--data-dir', str(directory), 'catalog', 'shop-mappings'])


class TestCatalogWorkflow(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = paths.current.data_dir
        self.addCleanup(paths.configure, previous)
        self.data = Path(directory.name)
        paths.configure(self.data)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        from sellerops.features.catalog import mapping, single_shop
        self.stack.enter_context(patch.object(mapping, 'main_shop', return_value=1))
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
        book.save(paths.current.price_book_xlsx)
        book.close()
        atomic_dump_json(paths.current.vc_known_json, {
            self.live_vc: {'cn': '当前商品', 'sku': 'SKU1', 'dp': 10},
            self.stale_vc: {'cn': '历史商品', 'sku': 'SKU2', 'dp': 20},
        })
        atomic_dump_json(paths.current.vc_override_json, {})
        atomic_dump_json(paths.current.vc_excluded_json, {})
        self.save_snapshot(1, [self.live])
        self.save_snapshot(2, [])
        atomic_dump_json(paths.current.status_json, {'shops': self.shops, 'errors': []})
        # The old local table still contains the item that has since left shop 2.
        single_shop.build_single_shop_xlsx(1, 'live', rows=[self.live])
        single_shop.build_single_shop_xlsx(2, 'empty', rows=[self.stale])

    def save_snapshot(self, sid, rows):
        atomic_dump_json(paths.shop_json_path(sid), {'shopId': sid, 'total': len(rows), 'rows': rows})

    def run_cli(self, *argv):
        from sellerops.cli.main import main
        return main(['--data-dir', str(self.data), *argv])

    def mapping_vendor_codes(self):
        book = load_workbook(paths.current.mapping_xlsx, read_only=True, data_only=True)
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
        self.assertEqual(self.run_cli('catalog', 'merge'), 0)
        self.assert_shop_empty(2, 'empty')
        self.assertEqual(self.mapping_vendor_codes(), {self.live_vc})
        # Removing an inactive item from coverage must preserve historical ownership.
        registry = json.loads(Path(paths.current.vc_known_json).read_text(encoding='utf8'))
        self.assertIn(self.stale_vc, registry)

    def test_merge_all_empty_shops_rebuilds_valid_empty_workbooks(self):
        self.save_snapshot(1, [])
        self.assertEqual(self.run_cli('catalog', 'merge'), 0)
        self.assert_shop_empty(1, 'live')
        self.assert_shop_empty(2, 'empty')
        self.assertEqual(self.mapping_vendor_codes(), set())

    def test_missing_snapshot_fails_before_refreshing_any_shop_table(self):
        first = Path(paths.shop_mapping_xlsx(1, 'live'))
        second = Path(paths.shop_mapping_xlsx(2, 'empty'))
        before = (first.read_bytes(), second.read_bytes())
        Path(paths.shop_json_path(2)).unlink()
        self.assertEqual(self.run_cli('catalog', 'merge'), 1)
        self.assertEqual((first.read_bytes(), second.read_bytes()), before)
        self.assertFalse(Path(paths.current.mapping_xlsx).exists())

    def test_corrupt_snapshot_is_not_treated_as_empty_or_silently_merged(self):
        first = Path(paths.shop_mapping_xlsx(1, 'live'))
        second = Path(paths.shop_mapping_xlsx(2, 'empty'))
        before = (first.read_bytes(), second.read_bytes())
        Path(paths.shop_json_path(2)).write_text('{broken', encoding='utf8')
        self.assertEqual(self.run_cli('catalog', 'merge'), 1)
        self.assertEqual((first.read_bytes(), second.read_bytes()), before)
        self.assertFalse(Path(paths.current.mapping_xlsx).exists())

    def test_missing_snapshot_of_archived_shop_does_not_block_live_merge(self):
        archive = Path(paths.current.shops_archive_dir)
        archive.mkdir(parents=True)
        Path(paths.shop_mapping_xlsx(2, 'empty')).rename(archive / 'shop_2_empty.xlsx')
        Path(paths.shop_json_path(2)).unlink()
        self.assertEqual(self.run_cli('catalog', 'merge'), 0)
        self.assertEqual(self.mapping_vendor_codes(), {self.live_vc})

    def formal_files(self):
        data = paths.current
        files = [Path(path) for path in (data.mapping_xlsx, data.vc_known_json,
                                         data.vc_override_json, data.vc_excluded_json)]
        files.extend(Path(data.shops_dir).glob('*.xlsx'))
        return {path.name: path.read_bytes() for path in files if path.exists()}

    def test_shop_refresh_checks_only_selected_shop_before_saving(self):
        other = Path(paths.shop_mapping_xlsx(2, 'empty'))
        old_table = other.read_bytes()
        Path(paths.shop_json_path(2)).unlink()
        # Bare lists are an existing snapshot format.
        atomic_dump_json(paths.shop_json_path(1), [self.live])
        self.assertEqual(self.run_cli('catalog', 'shop-mappings', '--shop-id', '1'), 0)
        before = self.formal_files()
        for scope in ([], ['--shop-id', '2'], ['--shop-id', '99']):
            self.assertEqual(self.run_cli('catalog', 'shop-mappings', *scope), 1)
            self.assertEqual(self.formal_files(), before)
        self.assertEqual(other.read_bytes(), old_table)
        self.save_snapshot(2, [])
        self.assertEqual(self.run_cli('catalog', 'shop-mappings', '--shop-id', '2'), 0)
        self.assert_shop_empty(2, 'empty')

    def test_bad_inputs_never_publish_a_review_or_rename(self):
        review = self.data / 'review.json'
        atomic_dump_json(review, [{'vc': self.live_vc, 'action': 'exclude'}])
        before = self.formal_files()
        for doc in ({'rows': [], 'shopId': 1}, {'rows': [], 'total': 7},
                    {'rows': [{'shopId': 1}]}, {'total': 0}):
            atomic_dump_json(paths.shop_json_path(2), doc)
            for action in [('merge', str(review)), ('rename', '--vc', self.live_vc, '--cn', '新名')]:
                self.assertEqual(self.run_cli('catalog', *action), 1)
                self.assertEqual(self.formal_files(), before)
        self.save_snapshot(2, [])
        missing = str(self.data / 'missing.json')
        for action in [('merge', missing), ('rename', '--file', missing)]:
            self.assertEqual(self.run_cli('catalog', *action), 1)
            self.assertEqual(self.formal_files(), before)
        from sellerops.features.catalog import mapping
        with patch.object(mapping, 'build_xlsx', side_effect=OSError('fixture: workbook generation failed')):
            self.assertEqual(self.run_cli('catalog', 'merge', str(review)), 1)
        self.assertEqual(self.formal_files(), before)
        self.assertFalse((self.data / 'state/.mapping-work').exists())
        self.assertFalse((self.data / 'state/.mapping-pending.json').exists())

    def test_merge_requires_an_active_shop_even_after_archiving_all_tables(self):
        archive = Path(paths.current.shops_archive_dir)
        archive.mkdir()
        for book in Path(paths.current.shops_dir).glob('*.xlsx'):
            book.rename(archive / book.name)
        before = self.formal_files()
        self.assertEqual(self.run_cli('catalog', 'merge'), 1)
        self.assertEqual(self.formal_files(), before)

    def test_cold_start_preserves_history_and_writes_only_after_preparation(self):
        from sellerops.features.catalog import mapping
        atomic_dump_json(paths.current.vc_excluded_json, {'HISTORY': '历史排除'})
        atomic_dump_json(paths.current.vc_override_json, {self.live_vc: {'cn': '人工名'}})
        self.assertEqual(self.run_cli('catalog', 'merge'), 0)
        Path(paths.current.vc_known_json).unlink()
        Path(paths.current.vc_excluded_json).unlink()
        before = self.formal_files()
        with patch.object(mapping, 'build_xlsx', side_effect=OSError('fixture: full table')):
            self.assertEqual(self.run_cli('catalog', 'merge'), 1)
        self.assertEqual(self.formal_files(), before)
        self.assertEqual(self.run_cli('catalog', 'merge'), 0)
        state = json.loads(Path(paths.current.vc_known_json).read_text(encoding='utf8'))
        self.assertEqual(state[self.live_vc]['source'], '历史映射表迁移')
        self.assertEqual(state[self.live_vc]['cn'], '人工名')
        excluded = json.loads(Path(paths.current.vc_excluded_json).read_text(encoding='utf8'))
        self.assertEqual(excluded['HISTORY'], '历史排除')

    def block_replace(self, filename, *, after=False):
        import os
        replace = os.replace
        work = self.data / 'state/.mapping-work'

        def interrupt(src, dst):
            matches = Path(src).parent == work and Path(dst).name == filename
            if matches and not after:
                raise PermissionError('fixture: open workbook')
            replace(src, dst)
            if matches:
                raise OSError('fixture: interruption after replacement')
        return patch.object(os, 'replace', side_effect=interrupt)

    def test_resume_at_first_middle_and_last_replacement_without_original_input(self):
        from sellerops.features.catalog import mapping
        first = Path(paths.shop_mapping_xlsx(1, 'live'))
        second = Path(paths.shop_mapping_xlsx(2, 'empty'))
        total = Path(paths.current.mapping_xlsx)
        state = self.data / 'state'
        for target in (first, second, total):
            with self.subTest(target=target.name):
                rename = self.data / 'rename.json'
                atomic_dump_json(rename, [{'vc': self.live_vc, 'cn': target.stem}])
                args = ('catalog', 'rename', '--file', str(rename))
                with self.block_replace(target.name, after=target == total):
                    self.assertEqual(self.run_cli(*args), 1)
                self.assertTrue((state / '.mapping-pending.json').exists())
                rename.unlink()
                with patch.object(mapping, 'build_xlsx', side_effect=AssertionError('重复生成')):
                    self.assertEqual(self.run_cli(*args), 0)
                self.assertFalse((state / '.mapping-work').exists())
                self.assertFalse((state / '.mapping-pending.json').exists())
                overrides = json.loads(Path(paths.current.vc_override_json).read_text(encoding='utf8'))
                self.assertEqual(overrides[self.live_vc]['cn'], target.stem)
                from sellerops.features.catalog import single_shop
                self.assertEqual(single_shop.load_single_shop_rows(1, 'live')[0][self.live_vc]['cn'], target.stem)

    def test_next_process_recovers_after_exit_with_an_os_lock_held(self):
        import multiprocessing
        process = multiprocessing.get_context('spawn').Process(target=exit_after_first_shop_save, args=(self.data,))
        process.start()
        try:
            process.join(15)
            self.assertEqual(process.exitcode, 23)
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
        Path(paths.shop_json_path(2)).unlink()
        self.assertEqual(self.run_cli('catalog', 'shop-mappings'), 0)
        self.assert_shop_empty(2, 'empty')
        self.assertFalse((self.data / 'state/.mapping-pending.json').exists())

    def test_updated_review_at_same_path_runs_after_recovery(self):
        review = self.data / 'review.json'
        args = ('catalog', 'merge', str(review))
        atomic_dump_json(review, [{'vc': self.live_vc, 'action': 'exclude'}])
        with self.block_replace('价格映射表.xlsx'):
            self.assertEqual(self.run_cli(*args), 1)
        atomic_dump_json(review, [{'vc': self.stale_vc, 'action': 'mapped', 'bossCn': '新归属'},
                                  {'vc': self.live_vc, 'action': 'pending'}])
        self.assertEqual(self.run_cli(*args), 0)
        self.assertIn(self.live_vc, json.loads(Path(paths.current.vc_excluded_json).read_text(encoding='utf8')))
        self.assertEqual(json.loads(Path(paths.current.vc_known_json).read_text(encoding='utf8'))[self.stale_vc]['cn'], '新归属')

    def test_legacy_import_finishes_pending_rename_then_publishes_its_own_workbook(self):
        with self.block_replace('价格映射表.xlsx'):
            self.assertEqual(self.run_cli('catalog', 'rename', '--vc', self.live_vc, '--cn', '修正名'), 1)
        legacy = self.data / 'legacy.json'
        atomic_dump_json(legacy, {'rows': [{'idx': 1, 'sku': 'SKU1', 'cn': '旧格式',
                                            'doublePrice': 10, 'vendorCodes': ['IMPORTED']}]})
        self.assertEqual(self.run_cli('catalog', 'import-mapping', str(legacy)), 0)
        self.assertEqual(self.mapping_vendor_codes(), {'IMPORTED'})
        known = json.loads(Path(paths.current.vc_known_json).read_text(encoding='utf8'))
        self.assertEqual(known[self.live_vc]['cn'], '修正名')
        self.assertFalse((self.data / 'state/.mapping-pending.json').exists())
        before = self.formal_files()
        atomic_dump_json(legacy, [{'vc': 'REVIEW', 'action': 'pending'}])
        self.assertEqual(self.run_cli('catalog', 'import-mapping', str(legacy)), 1)
        self.assertEqual(self.formal_files(), before)

    def test_damaged_pending_batch_stops_other_writers_before_replacement(self):
        first = Path(paths.shop_mapping_xlsx(1, 'live'))
        before = self.formal_files()
        with self.block_replace(first.name):
            self.assertEqual(self.run_cli('catalog', 'merge'), 1)
        record = self.data / 'state/.mapping-pending.json'
        original = record.read_bytes()
        corrupt = json.loads(original)
        corrupt['files']['credentials.json'] = '0' * 64
        atomic_dump_json(record, corrupt)
        self.assertEqual(self.run_cli('catalog', 'shop-mappings'), 1)
        self.assertEqual(self.formal_files(), before)
        record.write_bytes(original)
        (self.data / 'state/.mapping-work/价格映射表.xlsx').write_bytes(b'broken')
        self.assertEqual(self.run_cli('catalog', 'import-mapping', 'unused.json'), 1)
        self.assertEqual(self.formal_files(), before)
        self.assertTrue(record.exists())

    def test_partial_fetch_failure_returns_nonzero_saves_errors_and_keeps_old_snapshot(self):
        from sellerops.integrations.bcs.client import client as bcs
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
            self.assertEqual(self.run_cli('catalog', 'fetch', '--source', 'bcs', '--no-sync'), 1)
        self.assertEqual(visited, [1, 2, 3])
        self.assertEqual(Path(paths.shop_json_path(2)).read_bytes(), old_failed)
        self.assertEqual(json.loads(Path(paths.shop_json_path(1)).read_text(encoding='utf8'))['rows'], [self.live])
        self.assertEqual(json.loads(Path(paths.shop_json_path(3)).read_text(encoding='utf8'))['rows'], [])
        status = json.loads(Path(paths.current.status_json).read_text(encoding='utf8'))
        self.assertEqual([error['id'] for error in status['errors']], [2])
        sync.assert_not_called()
        sync_one.assert_not_called()

    def test_fetch_defaults_to_bcs_snapshot_and_keeps_open_api_explicit(self):
        from sellerops.integrations.bcs.client import client as bcs
        from sellerops.features.catalog import products
        with patch.object(bcs, 'fetch_shop_list', return_value=self.shops), \
                patch.object(bcs, 'fetch_shop_products',
                             side_effect=lambda sid, filter_type: [self.live] if sid == 1 else []) as legacy, \
                patch.object(bcs, 'sync_shops_parallel') as sync, \
                patch.object(bcs, 'fetch_open_api_shops') as open_api, \
                patch.object(products.time, 'sleep'):
            self.assertEqual(self.run_cli('catalog', 'fetch'), 0)
        self.assertEqual(legacy.call_count, len(self.shops))
        self.assertEqual(sync.call_count, 1)
        open_api.assert_not_called()
        doc = json.loads(Path(paths.shop_json_path(1)).read_text(encoding='utf8'))
        self.assertEqual(doc['source'], 'bcs')

    def test_successful_fetch_including_empty_shop_returns_zero_and_clears_old_errors(self):
        from sellerops.integrations.bcs.client import client as bcs
        from sellerops.features.catalog import products
        atomic_dump_json(paths.current.status_json, {'shops': self.shops, 'errors': [{'id': 2, 'error': 'previous failure'}]})
        with patch.object(bcs, 'fetch_shop_list', return_value=self.shops), \
                patch.object(bcs, 'fetch_shop_products', side_effect=lambda sid, filter_type: [self.live] if sid == 1 else []), \
                patch.object(bcs, 'sync_shops_parallel') as sync, \
                patch.object(bcs, 'sync_shop') as sync_one, patch.object(products.time, 'sleep'):
            self.assertEqual(self.run_cli('catalog', 'fetch', '--source', 'bcs', '--no-sync'), 0)
        status = json.loads(Path(paths.current.status_json).read_text(encoding='utf8'))
        self.assertEqual(status['errors'], [])
        self.assertEqual(json.loads(Path(paths.shop_json_path(2)).read_text(encoding='utf8'))['rows'], [])
        sync.assert_not_called()
        sync_one.assert_not_called()

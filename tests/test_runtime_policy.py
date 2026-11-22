"""Portable original-data layout and the no-post-write-query contract."""
import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def inspect_portable_project(project, working_directory, queue):
    """Fresh process: load a moved release, with only its sibling data directory."""
    os.environ.pop('SELLEROPS_DATA_DIR', None)
    os.chdir(working_directory)
    sys.argv = [str(Path(project) / 'run.py'), 'migrate-legacy']
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            runpy.run_path(sys.argv[0], run_name='__main__')
        except SystemExit as exc:
            if exc.code:
                raise
    from sellerops.settings import paths, credentials
    from sellerops.persistence.mappings import MappingRepository
    from sellerops.persistence.snapshots import ProductSnapshotRepository
    queue.put({
        'data': paths.current.data_dir,
        'credential': credentials.get().token,
        'products': len(MappingRepository.load_price_book()),
        'mapping': len(MappingRepository.load_mapping_state()[0]),
        'shops': ProductSnapshotRepository.shop_ids_from_disk(),
        'snapshot': len(ProductSnapshotRepository.load_shop_rows(42)),
        'source': str(Path(sys.modules['sellerops'].__file__).resolve()),
    })


def run_isolated_mapping(directory, barrier, queue):
    from sellerops.cli.main import main
    from openpyxl import load_workbook
    from sellerops.persistence.snapshots import ProductSnapshotRepository
    from sellerops.settings import credentials, paths

    output = directory / '..' / f'{directory.name}_isolated.xlsx'
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        migrated = main(['--data-dir', str(directory), 'migrate-legacy'])
        barrier.wait(timeout=15)
        done = main(['--data-dir', str(directory), 'report', 'mapping', '--shop-id', '42', '--out', str(output)])
    book = load_workbook(output, read_only=True)
    try:
        mapped = [row[1] for row in list(book.worksheets[0].values)[1:]]
    finally:
        book.close()
    indexed = ProductSnapshotRepository.load_shop_rows(42)
    queue.put((paths.current.data_dir, migrated, done, credentials.get().token, mapped, list(indexed)))


class TestDataLayout(unittest.TestCase):
    def test_overlapping_cli_cannot_switch_paths_and_sequential_cli_still_can(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from sellerops.cli import main
        from sellerops.settings import paths
        previous = paths.current.data_dir
        self.addCleanup(paths.configure, previous)
        entered, release = Event(), Event()

        def wait_for_second(args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('second command did not finish')
            return 0

        with tempfile.TemporaryDirectory() as root, ThreadPoolExecutor(1) as worker:
            a, b = (Path(root) / name for name in ('a', 'b'))
            def command(directory):
                return ['--data-dir', str(directory), 'task', 'schedule']
            with patch.object(main, 'dispatch', side_effect=wait_for_second):
                first = worker.submit(main.main, command(a))
                try:
                    self.assertTrue(entered.wait(5))
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main.main(command(b)), 1)
                    self.assertEqual(Path(paths.current.data_dir), a)
                finally:
                    release.set()
                self.assertEqual(first.result(5), 0)
            with patch.object(main, 'dispatch', return_value=0):
                self.assertEqual(main.main(command(b)), 0)
            self.assertEqual(Path(paths.current.data_dir), b)

    def test_parallel_processes_migrate_and_export_only_their_own_mapping(self):
        from openpyxl import Workbook
        ctx = multiprocessing.get_context('spawn')
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        with tempfile.TemporaryDirectory() as root:
            processes, expected = [], []
            for name in ('A', 'B'):
                directory = Path(root) / name
                (directory / 'products').mkdir(parents=True)
                (directory / 'state').mkdir()
                vc = 'BCS-TEST-' + name
                (directory / 'credentials.json').write_text(json.dumps({'bcs': {'token': name}}), encoding='utf8')
                (directory / 'state/vc_known.json').write_text(json.dumps({vc: {'cn': name}}), encoding='utf8')
                (directory / 'products/shop42_products_all.json').write_text(json.dumps(
                    {'shopId': 42, 'rows': [{'nmId': 123, 'vendorCode': vc}]}), encoding='utf8')
                book = Workbook()
                book.active.title = 'Sheet1'
                book.active.append(['卖家SKU', '产品中文名', '实际售价', 'vendorCode前缀码'])
                book.active.append(['SKU', name, 12, 'TEST'])
                book.save(directory / '商品价格表.xlsx')
                book.close()
                process = ctx.Process(target=run_isolated_mapping, args=(directory, barrier, queue))
                process.start()
                processes.append(process)
                expected.append((str(directory), 0, 0, name, [vc], [vc]))
            try:
                for process in processes:
                    process.join(25)
                    self.assertEqual(process.exitcode, 0)
                self.assertCountEqual([queue.get(timeout=2) for _ in processes], expected)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join()
                queue.close()

    def test_moved_project_reads_original_layout_without_path_configuration(self):
        from openpyxl import Workbook
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'release'
            shutil.copytree(project_root / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(project_root / 'run.py', project / 'run.py')
            data = project / 'data'
            (data / 'products').mkdir(parents=True)
            (data / 'credentials.json').write_text(json.dumps({'bcs': {'token': 'fixture-token'}}), encoding='utf8')
            (data / 'products/shop42_products_all.json').write_text(json.dumps({'rows': [{'vendorCode': 'BCS-TEST-123', 'nmId': 123}]}), encoding='utf8')
            book = Workbook()
            book.active.title = 'Sheet1'
            book.active.append(['卖家SKU', '产品中文名', '实际售价'])
            book.active.append(['SKU1', '测试商品', 10])
            book.save(data / '商品价格表.xlsx')
            book.close()
            book = Workbook()
            book.active.title = '映射总表'
            book.active.append(['产品中文名', 'vendorCode', '实际售价'])
            book.active.append(['测试商品', 'BCS-TEST-123', 10])
            book.save(data / '价格映射表.xlsx')
            book.close()
            ctx = multiprocessing.get_context('spawn')
            queue = ctx.Queue()
            process = ctx.Process(target=inspect_portable_project, args=(str(project), directory, queue))
            process.start()
            result = queue.get(timeout=30)
            process.join(15)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(Path(result['data']), data)
            self.assertEqual(result['credential'], 'fixture-token')
            self.assertEqual((result['products'], result['mapping'], result['snapshot']), (1, 1, 1))
            self.assertEqual(result['shops'], [42])
            self.assertTrue(Path(result['source']).is_relative_to(project / 'src'))
            queue.close()

    def test_data_dir_option_updates_file_argument_defaults_before_dispatch(self):
        from sellerops.cli import main
        from sellerops.settings import paths
        previous = paths.current.data_dir
        with tempfile.TemporaryDirectory() as directory:
            try:
                with patch.object(main, 'dispatch', return_value=0) as dispatch:
                    self.assertEqual(main.main(['--data-dir', directory, 'support', 'ai-test']), 0)
                self.assertEqual(Path(dispatch.call_args.args[0].qa), Path(directory) / 'ai_test_qa.json')
            finally:
                paths.configure(previous)

    def test_snapshot_cache_does_not_mix_two_data_directories(self):
        from sellerops.settings import paths
        from sellerops.persistence.snapshots import ProductSnapshotRepository as repo
        previous = paths.current.data_dir
        with tempfile.TemporaryDirectory() as directory:
            try:
                for name in ['first', 'second']:
                    paths.configure(Path(directory) / name)
                    repo.save_shop_products(42, [{'vendorCode': name}])
                    self.assertEqual(list(repo.load_shop_rows(42)), [name])
            finally:
                paths.configure(previous)


class TestNoPostWriteQueries(unittest.TestCase):
    def test_write_commands_do_not_accept_verification_or_chained_sync(self):
        from sellerops.cli.main import build_parser
        parser = build_parser()
        from sellerops.cli.main import leaf_commands
        commands = [name for name, _ in leaf_commands(parser) if name.startswith(("product ", "listing "))]
        for command in commands:
            for flag in ['--verify', '--no-verify', '--sync']:
                argv = [*command.split(), flag]
                with self.subTest(command=command, flag=flag), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exc:
                        parser.parse_args(argv)
                    self.assertEqual(exc.exception.code, 2)

    def test_banned_does_not_requery_after_submission(self):
        from sellerops.features.products import banned
        args = SimpleNamespace(apply=True, limit=0, no_verify=False)
        submitted = False

        def read(*_):
            self.assertFalse(submitted, 'a read occurred after submission')
            return [{'nmID': 123, 'vendorCode': 'BCS-TEST-123'}]

        def submit(*_):
            nonlocal submitted
            submitted = True
            return {"details": [{"nmID": 123, "ok": True, "msg": "OK"}]}

        with patch.object(banned, 'fetch_banned_cards', side_effect=read) as fetch, \
                patch.object(banned, 'move_in_session', side_effect=submit), \
                patch.object(banned.wb_api, 'request') as count, \
                patch.object(banned, '_card_cn', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            result = banned.process_shop({'shopId': 42, 'shopName': 'fixture'}, 42, Mock(), args, [])
        self.assertEqual(result['moved'], 1)
        self.assertEqual(fetch.call_count, 1)
        count.assert_not_called()

    def test_publish_interfaces_stop_after_acceptance(self):
        from sellerops.features.publishing import bulk_publish, card_publish
        from sellerops.domain.models import PublishItem
        from sellerops.features.catalog import products, mapping_sync
        from sellerops.integrations.bcs.client import client as bcs
        item = PublishItem(nm_id=123, vendor_code='BCS-TEST-123', prefix='BCS-TEST', price=10,
                           length=10, width=20, height=30, weight=0.5,
                           target_shops=[42], extra={'card_info': {'data': {'subject_id': 1}}, 'wb_detail': {'subjectId': 1}})
        with patch.object(products, 'fetch_all') as fetch, patch.object(bcs, 'sync_shops_parallel') as sync, \
                patch.object(mapping_sync, 'merge') as merge, contextlib.redirect_stdout(io.StringIO()):
            for module in [bulk_publish, card_publish]:
                from sellerops.settings import paths
                self.addCleanup(paths.configure, paths.current.data_dir)
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                paths.configure(folder.name)
                with patch.object(bcs, 'fetch_shop_list', return_value=[{'id': 42}]), \
                        patch.object(module, 'resolve_warehouse', return_value=100), \
                        patch.object(module, 'load_records', return_value={}), \
                        patch.object(bcs, 'batch_push_products', return_value={'code': 200, 'data': {'taskId': 1}}), \
                        patch.object(bcs, 'post') as post:
                    post.return_value = {'code': 200}
                    result = module.execute_publish([item], apply=True, writer=Mock(), check_exists=False)
                    self.assertEqual(result['ok'], 1)
            fetch.assert_not_called()
            sync.assert_not_called()
            merge.assert_not_called()

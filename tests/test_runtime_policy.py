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
    sys.argv = [str(Path(project) / 'run.py'), 'daily', 'morning']
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
        'data': paths.DATA_DIR,
        'credential': credentials.get().token,
        'products': len(MappingRepository.load_price_book()),
        'mapping': len(MappingRepository.load_mapping_state()[0]),
        'shops': ProductSnapshotRepository.shop_ids_from_disk(),
        'snapshot': len(ProductSnapshotRepository.load_shop_rows(42)),
        'source': str(Path(sys.modules['sellerops'].__file__).resolve()),
    })


class TestDataLayout(unittest.TestCase):
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
        previous = paths.DATA_DIR
        with tempfile.TemporaryDirectory() as directory:
            try:
                with patch.object(main, 'dispatch', return_value=0) as dispatch:
                    self.assertEqual(main.main(['--data-dir', directory, 'ai-test']), 0)
                self.assertEqual(Path(dispatch.call_args.args[0].qa), Path(directory) / 'ai_test_qa.json')
            finally:
                paths.configure(previous)

    def test_snapshot_cache_does_not_mix_two_data_directories(self):
        from sellerops.settings import paths
        from sellerops.persistence.snapshots import ProductSnapshotRepository as repo
        previous = paths.DATA_DIR
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
        commands = ['price', 'price-bcs', 'stock', 'stock-bcs', 'trash', 'trash-bcs',
                    'discount', 'discount-bcs', 'publish', 'create-cards', 'replicate',
                    'import-publish', 'dimension', 'banned', 'clean']
        for command in commands:
            for flag in ['--verify', '--no-verify', '--sync']:
                argv = [command, *(('fixture.xlsx',) if command == 'import-publish' else ()), flag]
                with self.subTest(command=command, flag=flag), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exc:
                        parser.parse_args(argv)
                    self.assertEqual(exc.exception.code, 2)

    def test_banned_does_not_requery_after_submission(self):
        from sellerops.features.listings import banned
        args = SimpleNamespace(apply=True, limit=0, no_verify=False)
        submitted = False

        def read(*_):
            self.assertFalse(submitted, 'a read occurred after submission')
            return [{'nmID': 123, 'vendorCode': 'BCS-TEST-123'}]

        def submit(*_):
            nonlocal submitted
            submitted = True
            return [123], {}

        with patch.object(banned, 'fetch_banned_cards', side_effect=read) as fetch, \
                patch.object(banned, 'move_to_trash', side_effect=submit), \
                patch.object(banned, 'count_banned') as count, \
                patch.object(banned, '_card_cn', return_value='fixture'), contextlib.redirect_stdout(io.StringIO()):
            result = banned.process_shop({'shopId': 42, 'shopName': 'fixture'}, 42, Mock(), args, [])
        self.assertEqual(result['moved'], 1)
        self.assertEqual(fetch.call_count, 1)
        count.assert_not_called()

    def test_price_stock_and_operations_finish_after_execution(self):
        from sellerops.cli.main import build_parser
        from sellerops.features.listings import prices, stocks, operations
        from sellerops.features.catalog import products, mapping_sync
        from sellerops.integrations.bcs import client as bcs
        fixture = Mock(root_version='fixture')
        fixture.wb_shop.return_value = {'shopId': 42}
        plan = [{'shopId': 42, 'items': [{'nmID': 123, 'vc': 'BCS-TEST-123', 'price': 10, 'chrtId': 456, 'amount': 1}]}]
        parser = build_parser()
        with patch.object(products, 'fetch_all') as fetch, patch.object(bcs, 'sync_shops_parallel') as sync, \
                patch.object(mapping_sync, 'run_merge') as merge, contextlib.redirect_stdout(io.StringIO()):
            for module, command in [(prices, 'price'), (stocks, 'stock')]:
                args = parser.parse_args([command, '--vc', 'BCS-TEST-123', '--apply', '--yes'])
                args.sync = True  # stale callers must not reactivate removed hooks
                planner = 'plan_price' if module is prices else 'build_plans'
                executor = 'exec_shop' if module is prices else 'exec_online'
                with patch.object(module, 'resolve_filters', return_value=(['BCS-TEST-123'], 'fixture')), \
                        patch.object(module, '_filter_shops', return_value=[42]), \
                        patch.object(module, 'load_state', return_value=({}, {}, [])), \
                        patch.object(module, planner, return_value=plan), \
                        patch.object(module, executor, return_value=(1, 0)) as execute, \
                        patch.object(module.wb_api, 'make_session'), \
                        patch.object(module.credentials, 'get', return_value=fixture), \
                        patch.object(module, '_append_csv'), patch.object(module, '_report_zero_items'):
                    self.assertEqual(module.run(args), 0)
                    execute.assert_called_once()
            for action in ['price', 'stock', 'trash']:
                args = parser.parse_args([{'price': 'price-bcs', 'stock': 'stock-bcs', 'trash': 'trash'}[action], '--apply', '--yes'])
                args.sync = True
                with patch.object(operations, 'resolve_filters', return_value=(['BCS-TEST-123'], 'fixture')), \
                        patch.object(operations, 'get_shops', return_value=[42]), \
                        patch.object(operations, 'load_state', return_value=({}, {}, [])), \
                        patch.object(operations, 'plan_' + action, return_value=plan), \
                        patch.object(operations, 'run_apply', return_value=(1, 0)) as execute:
                    operations.run(action, args)
                    execute.assert_called_once()
            fetch.assert_not_called()
            sync.assert_not_called()
            merge.assert_not_called()

    def test_publish_interfaces_stop_after_acceptance(self):
        from sellerops.features.listings import bulk_publish, card_publish
        from sellerops.domain.models import PublishItem
        from sellerops.features.catalog import products, mapping_sync
        from sellerops.integrations.bcs import client as bcs
        item = PublishItem(nm_id=123, vendor_code='BCS-TEST-123', prefix='BCS-TEST', price=10,
                           length=10, width=20, height=30, weight=0.5,
                           target_shops=[42], extra={'card_info': {'data': {'subject_id': 1}}, 'wb_detail': {'subjectId': 1}})
        with patch.object(products, 'fetch_all') as fetch, patch.object(bcs, 'sync_shops_parallel') as sync, \
                patch.object(mapping_sync, 'run_merge') as merge, contextlib.redirect_stdout(io.StringIO()):
            for module in [bulk_publish, card_publish]:
                with patch.object(bcs, 'fetch_shop_list', return_value=[{'id': 42}]), \
                        patch.object(module, 'resolve_warehouse', return_value=100), \
                        patch.object(module, 'load_records', return_value={}), \
                        patch.object(module, 'record_publish_success'), patch.object(module, 'save_records'), \
                        patch.object(bcs, 'batch_push_products', return_value={'code': 200, 'data': {'taskId': 1}}), \
                        patch.object(bcs, 'client', base_url='https://fixture.invalid') as client:
                    client.post.return_value = {'code': 200}
                    result = module.execute_publish([item], apply=True, writer=Mock(), check_exists=False)
                    self.assertEqual(result['ok'], 1)
            fetch.assert_not_called()
            sync.assert_not_called()
            merge.assert_not_called()

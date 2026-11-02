"""State integrity, process exclusion, and explicit execution contracts."""
import contextlib
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sellerops.core.errors import DataConsistencyError, StorageLockError
from sellerops.core.files import FileLock, atomic_dump_json, safe_load_json


def contend_for_lock(target, queue):
    try:
        with FileLock(target, timeout=0.15):
            queue.put('acquired')
    except StorageLockError:
        queue.put('timeout')


class TestStateIntegrity(unittest.TestCase):
    def test_json_roundtrip_and_missing_default(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'state.json'
            self.assertEqual(safe_load_json(p, {}), {})
            atomic_dump_json(p, {'商品': [1, 2]})
            self.assertEqual(safe_load_json(p), {'商品': [1, 2]})

    def test_corruption_is_not_treated_as_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'state.json'
            p.write_text('{broken', encoding='utf8')
            with self.assertRaises(DataConsistencyError):
                safe_load_json(p, {})

    def test_failed_replace_preserves_old_state_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'state.json'
            atomic_dump_json(p, {'old': True})
            with patch('sellerops.core.files.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    atomic_dump_json(p, {'new': True})
            self.assertEqual(safe_load_json(p), {'old': True})
            self.assertEqual({f.name for f in p.parent.iterdir()}, {'state.json', 'state.json.lock'})

    def test_reentrant_lock_for_read_modify_write(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'state.json'
            with FileLock(p):
                atomic_dump_json(p, [1])
                with FileLock(p):
                    self.assertEqual(safe_load_json(p), [1])

    def test_process_lock_blocks_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / 'shared.json')
            ctx = multiprocessing.get_context('spawn')
            queue = ctx.Queue()
            with FileLock(target):
                process = ctx.Process(target=contend_for_lock, args=(target, queue))
                process.start()
                self.assertEqual(queue.get(timeout=15), 'timeout')
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            process = ctx.Process(target=contend_for_lock, args=(target, queue))
            process.start()
            self.assertEqual(queue.get(timeout=15), 'acquired')
            process.join(15)
            self.assertEqual(process.exitcode, 0)
            queue.close()


class TestExecutionContracts(unittest.TestCase):
    def test_manual_reply_requires_apply(self):
        from sellerops.cli.main import build_parser
        from sellerops.features.support import questions
        args = build_parser().parse_args(['questions', '--reply', '你好', '--question-id', 'q1', '--no-detail'])
        with patch.object(questions.wb_api, 'make_session'), \
                patch.object(questions, 'fetch_questions', return_value=[{'id': 'q1'}]), \
                patch.object(questions, 'reply') as send, contextlib.redirect_stdout(io.StringIO()):
            questions.process_shop({'shopId': 1}, '', args, [], {})
            send.assert_not_called()
            args.apply = True
            questions.process_shop({'shopId': 1}, '', args, [], {})
            send.assert_called_once()

    def test_product_id_does_not_confuse_variant_number(self):
        from sellerops.core.utils import extract_wb_nm
        self.assertEqual(extract_wb_nm('BCS-ABCD-123456-2'), '123456')
        self.assertEqual(extract_wb_nm('BCS-ABCD-ozon-card-123456'), '123456')
        self.assertEqual(extract_wb_nm('BCS-ABCD-tag/123456-2'), '123456')
        self.assertIsNone(extract_wb_nm('invalid'))

    def test_invalid_discount_plans_fail_before_platform_calls(self):
        from sellerops.domain.models import DiscountPlan
        for values in ({'target_discount': -1}, {'chunk_size': 0}, {'chunk_size': 301}, {'limit': -1}):
            with self.assertRaises(ValueError):
                DiscountPlan(**values)

    def test_discount_partial_failure_returns_nonzero(self):
        from sellerops.cli.main import build_parser
        from sellerops.features.pricing import discounts
        from types import SimpleNamespace
        fixture = SimpleNamespace(shops=[{'shopId': 1}])
        with patch.object(discounts.credentials, 'get', return_value=fixture), \
                patch.object(discounts.Discounts, 'execute_plan', return_value={'total_failed': 1}):
            self.assertEqual(discounts.run_cli(build_parser().parse_args(['discount', '--apply'])), 1)

    def test_ambiguous_bcs_write_is_not_retried(self):
        from sellerops.integrations.bcs.client import BCSClient
        from sellerops.core.errors import NetworkError
        import requests
        client = BCSClient()
        with patch.object(client, '_headers', return_value={}), \
                patch('requests.request', side_effect=requests.exceptions.Timeout) as request:
            with self.assertRaises(NetworkError):
                client.request_json('POST', 'https://fixture.invalid')
            self.assertEqual(request.call_count, 1)

    def test_daily_and_schedule_preview_without_subprocess(self):
        from sellerops.cli.main import build_parser
        from sellerops.cli.binding import dispatch
        parser = build_parser()
        with patch('subprocess.run', side_effect=AssertionError('must preview')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch(parser.parse_args(['daily', 'morning'])), 0)
            self.assertEqual(dispatch(parser.parse_args(['schedule'])), 0)
            self.assertEqual(dispatch(parser.parse_args(['schedule', '--remove'])), 0)

    def test_daily_write_flag_propagates_and_shop_scope_is_kept(self):
        from sellerops.automation.daily import build_steps
        for argv in build_steps('morning', apply=True, shops='1,2'):
            self.assertIn('--apply', argv)
            self.assertEqual(argv[-2:], ['--shops', '1,2'])
        for argv in build_steps('morning'):
            self.assertNotIn('--apply', argv)

    def test_task_definition_preserves_interpreter_data_and_working_directory(self):
        from sellerops.automation.schedule import task_xml
        from xml.etree import ElementTree as ET
        task = ET.fromstring(task_xml('09:00', 'morning'))
        ns = {'s': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertTrue(task.find('.//s:Exec/s:Command', ns).text)
        args = task.find('.//s:Exec/s:Arguments', ns).text
        self.assertIn('--data-dir', args)
        self.assertIn('--apply', args)
        self.assertTrue(Path(task.find('.//s:WorkingDirectory', ns).text).exists())

    def test_paths_can_be_selected_without_creating_data(self):
        from sellerops.settings import paths
        previous = paths.DATA_DIR
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / 'unused'
            try:
                paths.configure(selected)
                self.assertEqual(Path(paths.CREDENTIALS_JSON), selected / 'credentials.json')
                self.assertFalse(paths.is_shop_archived(42))
                self.assertFalse(selected.exists())
                self.assertEqual(Path(paths.shop_mapping_xlsx(42, '../bad/name')).parent, selected / 'shops')
            finally:
                paths.configure(previous)

    def test_credentials_cache_follows_selected_directory(self):
        from sellerops.settings import paths, credentials
        previous = paths.DATA_DIR
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'credentials.json').write_text(json.dumps({'bcs': {'token': 'fixture'}}), encoding='utf8')
            try:
                paths.configure(p)
                self.assertEqual(credentials.get().token, 'fixture')
                paths.configure(p / 'other')
                self.assertEqual(credentials.get().token, '')
            finally:
                paths.configure(previous)

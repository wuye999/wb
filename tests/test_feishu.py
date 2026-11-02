"""Remote table pagination and temporary payload isolation with fake CLI results."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sellerops.integrations.feishu import tables


class TestFeishu(unittest.TestCase):
    def test_pagination_reads_every_page(self):
        offsets = []

        def fake_invoke(command):
            offset = int(command[command.index('--offset') + 1])
            offsets.append(offset)
            output = Path(command[command.index('--output') + 1])
            output.write_text(json.dumps({'record_id': str(offset)}) + '\n', encoding='utf8')
            return {'has_more': offset == 0, 'next_offset': 2000}

        with patch.object(tables, 'invoke', side_effect=fake_invoke), patch.object(tables.time, 'sleep'):
            rows = tables.list_records('base', 'table')
        self.assertEqual(offsets, [0, 2000])
        self.assertEqual([r['record_id'] for r in rows], ['0', '2000'])

    def test_nonadvancing_cursor_is_rejected(self):
        def fake_invoke(command):
            Path(command[command.index('--output') + 1]).write_text('{}\n', encoding='utf8')
            return {'has_more': True, 'next_offset': 0}

        with patch.object(tables, 'invoke', side_effect=fake_invoke):
            with self.assertRaisesRegex(RuntimeError, '没有前进'):
                tables.list_records('base', 'table')

    def test_empty_page_with_more_is_rejected(self):
        with patch.object(tables, 'invoke', return_value={'has_more': True, 'next_offset': 2000}):
            with self.assertRaises(RuntimeError):
                tables.list_records('base', 'table')

    def test_payload_cleanup_after_subprocess_failure(self):
        paths = []

        def fail(command, **kwargs):
            payload = Path(command[command.index('--json') + 1][1:])
            self.assertEqual(json.loads(payload.read_text(encoding='utf8')), {'value': '测试'})
            paths.append(payload)
            raise TimeoutError('fake timeout')

        with patch.object(tables.shutil, 'which', return_value='fixture-cli'), patch.object(tables.subprocess, 'run', side_effect=fail):
            with self.assertRaises(TimeoutError):
                tables.invoke(['+record-batch-create'], {'value': '测试'})
        self.assertFalse(paths[0].exists())

    def test_nonzero_exit_cannot_be_reported_as_success(self):
        result = SimpleNamespace(returncode=1, stdout='{"ok":true}')
        with patch.object(tables.shutil, 'which', return_value='fixture-cli'), patch.object(tables.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, '退出码'):
                tables.invoke(['+table-list'])

"""Exercise cleanup through the CLI with an isolated project and synthetic files."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sellerops.cli.main import main
from sellerops.features.system import clean
from sellerops.settings import paths


DAY_NS = 86_400 * 1_000_000_000
NOW = 1_800_000_000 * 1_000_000_000


class TaskCleanTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / 'data'
        self.logs = self.data / 'logs'
        self.exports = self.root / 'exports'
        self.scratch = self.root / '_scratch'
        self.addCleanup(paths.configure, paths.current.data_dir)

    def file(self, path, age=30):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
        mtime = NOW - age * DAY_NS
        os.utime(path, ns=(mtime, mtime))
        return path

    def cli(self, *options, data=None):
        stdout, stderr = StringIO(), StringIO()
        with patch.object(paths, 'PROJECT_ROOT', self.root), patch.object(clean, 'time_ns', return_value=NOW), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--data-dir', str(data or self.data), 'task', 'clean', *options, '--json'])
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_default_preview_reports_counts_without_removing_old_files_or_empty_dirs(self):
        old = self.file(self.logs / 'old' / 'receipt.csv')
        recent = self.file(self.logs / 'recent.csv', age=19)
        empty = self.exports / 'empty'
        empty.mkdir(parents=True)
        code, reply, _ = self.cli()
        self.assertEqual(code, 0, reply)
        self.assertEqual((reply['data']['mode'], reply['data']['days']), ('preview', 20))
        logs = reply['data']['groups']['data/logs']
        self.assertEqual((logs['files'], logs['directories'], logs['bytes'], logs['size_mb']),
                         (1, 1, 7, 7 / 1024**2))
        self.assertEqual(reply['data']['groups']['exports']['directories'], 1)
        self.assertTrue(all(path.exists() for path in (old, recent, empty)))
        self.assertFalse(self.scratch.exists())

    def test_apply_removes_only_expired_files_and_empty_subdirectories(self):
        old_files = [self.file(self.logs / 'old' / 'receipt.csv', age=11),
                     self.file(self.exports / 'fbs_shipments' / '20261101' / 'label.pdf', age=11),
                     self.file(self.scratch / 'old.csv', age=11)]
        empty = self.logs / 'empty'
        empty.mkdir()
        keep = [self.file(self.logs / 'mixed' / 'recent.csv', age=1),
                self.file(self.logs / 'mixed' / 'boundary.csv', age=10),
                self.file(self.exports / 'workbench' / 'nested' / 'latest.html'),
                self.file(self.scratch / 'README.md'),
                self.file(self.data / 'sellerops.db'),
                self.file(self.data / 'state' / 'status.json'),
                self.file(self.data / 'credentials.json'),
                self.file(self.data / '商品价格表.xlsx'),
                self.file(self.data / 'browser_profiles' / 'session'),
                self.file(self.data / 'ai_test_qa.json'),
                self.file(self.root / '_archive' / 'old.txt')]

        code, reply, _ = self.cli('--days', '10', '--apply')
        self.assertEqual(code, 0, reply)
        self.assertEqual(reply['data']['mode'], 'apply')
        groups = reply['data']['groups']
        self.assertEqual([(row['files'], row['directories'], row['errors']) for row in groups.values()],
                         [(1, 2, 0), (1, 2, 0), (1, 0, 0)])
        self.assertFalse(any(path.exists() for path in [*old_files, empty, old_files[0].parent,
                                                       old_files[1].parent.parent]))
        self.assertTrue(all(path.read_bytes() == b'fixture' for path in keep))
        self.assertTrue(all(path.is_dir() for path in (self.logs, self.exports, self.scratch)))

    def test_custom_data_directory_excludes_project_scratch(self):
        scratch_file = self.file(self.scratch / 'old.txt')
        selected = self.root / 'isolated' / 'data'
        selected_log = self.file(selected / 'logs' / 'old.txt')
        code, reply, _ = self.cli('--apply', data=selected)
        self.assertEqual(code, 0, reply)
        self.assertNotIn('_scratch', reply['data']['groups'])
        self.assertFalse(selected_log.exists())
        self.assertTrue(scratch_file.is_file())

    def test_refuses_when_exports_is_the_data_directory(self):
        private = self.file(self.exports / 'sellerops.db')
        code, reply, _ = self.cli('--apply', data=self.exports)
        self.assertEqual(code, 1, reply)
        self.assertTrue(private.is_file())

    def test_invalid_days_are_rejected_before_cleaning(self):
        old = self.file(self.logs / 'old.txt')
        for days in ('0', '-1', '1.5'):
            with self.subTest(days=days):
                code, reply, _ = self.cli('--days', days, '--apply')
                self.assertEqual(code, 2, reply)
                self.assertFalse(reply['ok'])
                self.assertTrue(old.is_file())

    def test_linked_roots_and_entries_are_skipped_without_reading_targets(self):
        external = self.file(self.root / 'outside' / 'old.txt')
        self.logs.mkdir(parents=True)
        link = self.logs / 'linked-directory'
        linked = (link, self.exports)
        probes = [external]
        if os.name == 'nt':
            # Standard Windows users may lack permission to create symbolic links.
            probes.extend(self.file(path / 'old.txt') for path in linked)
            lstat = Path.lstat

            def with_symlink_mode(path, *args, **kwargs):
                if path in linked:
                    return SimpleNamespace(st_mode=stat.S_IFLNK)
                return lstat(path, *args, **kwargs)

            metadata = patch.object(Path, 'lstat', with_symlink_mode)
        else:
            for path in linked:
                path.symlink_to(external.parent, target_is_directory=True)
            metadata = nullcontext()

        with metadata:
            code, reply, output = self.cli('--apply')
        self.assertEqual(code, 0, reply)
        groups = reply['data']['groups']
        self.assertEqual(groups['data/logs']['skipped_reparse_points'], 1)
        self.assertEqual(groups['exports']['skipped_reparse_points'], 1)
        self.assertIn('已跳过 1 个符号链接/目录联接，请人工确认', output)
        self.assertTrue(all(path.is_dir() for path in linked))
        self.assertTrue(all(path.is_file() for path in probes))

    def test_windows_reparse_attribute_blocks_descent_into_directory(self):
        old = self.file(self.logs / 'reparse' / 'old.txt')
        reparse = old.parent
        lstat = Path.lstat

        def with_reparse_attribute(path, *args, **kwargs):
            if path == reparse:
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return lstat(path, *args, **kwargs)

        with patch.object(Path, 'lstat', with_reparse_attribute):
            code, reply, _ = self.cli('--apply')
        self.assertEqual(code, 0, reply)
        self.assertEqual(reply['data']['groups']['data/logs']['skipped_reparse_points'], 1)
        self.assertTrue(old.is_file())

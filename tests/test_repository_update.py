"""Repository update checks with fake Git metadata, HTTP and subprocesses."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from sellerops.cli.main import main
from sellerops.features.system import repository_update
from sellerops.integrations import github_compare
from sellerops.settings import paths


HEAD = '1' * 40
NEW_HEAD = '2' * 40
UPDATE = {'status': 'update_available', 'ahead_by': 3}


class RepositoryUpdateTest(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.git = self.root / '.git'
        self.loose_main = self.git / 'refs/heads/main'
        self.loose_main.parent.mkdir(parents=True)
        self.loose_main.write_text(HEAD + '\n', encoding='ascii')
        (self.git / 'HEAD').write_text('ref: refs/heads/main\n', encoding='ascii')
        self.data = self.root / 'data'
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(self.data)
        self.cache = Path(paths.current.repository_update_json)
        self.enterContext(patch.object(paths, 'PROJECT_ROOT', self.root))
        self.git_available = self.enterContext(patch.object(repository_update.shutil, 'which', return_value='git'))
        self.clock = self.enterContext(patch.object(repository_update, 'time', return_value=1_796_000_000))
        self.probe = self.enterContext(patch.object(repository_update, '_probe', return_value=UPDATE))

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(['--data-dir', str(self.data), 'doctor', *args, '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        return json.loads(stdout.getvalue()), stderr.getvalue()

    def test_doctor_auto_checks_uses_cache_and_supports_force(self):
        reply, terminal = self.cli()
        update = reply['data']['repository_update']
        self.assertEqual(update, {**UPDATE, 'source': 'live',
                                  'checked_at': datetime.fromtimestamp(self.clock.return_value, timezone.utc).isoformat()})
        self.assertFalse(reply['data']['online_checked'])
        self.assertIn('git pull --ff-only https://github.com/wuye999/wb.git main', terminal)
        self.assertTrue(self.cache.is_file())
        self.probe.assert_called_once_with(HEAD)

        self.clock.return_value += 23 * 3600
        reply, terminal = self.cli()
        self.assertEqual(reply['data']['repository_update'], {**update, 'source': 'cache'})
        self.assertIn('[更新提醒]', terminal)
        self.probe.assert_called_once()

        self.probe.return_value = {'status': 'diverged', 'ahead_by': 1}
        reply, terminal = self.cli('--check-updates')
        self.assertEqual(reply['data']['repository_update']['status'], 'diverged')
        self.assertEqual(reply['data']['repository_update']['source'], 'live')
        self.assertNotIn('[更新提醒]', terminal)
        self.assertFalse(reply['data']['online_checked'])
        self.assertEqual(self.probe.call_count, 2)

    def test_24_hour_expiry_and_rejects_wrong_identity_or_bad_cache(self):
        self.assertEqual(repository_update.check()['source'], 'live')
        self.clock.return_value += 24 * 3600
        self.assertEqual(repository_update.check()['source'], 'live')
        record = json.loads(self.cache.read_text(encoding='utf-8'))
        for key, value in (('project_root', '/old-checkout'), ('remote', 'https://example.invalid/'),
                           ('branch', 'topic'), ('head', NEW_HEAD), ('version', 0)):
            with self.subTest(key=key):
                self.cache.write_text(json.dumps({**record, key: value}), encoding='utf-8')
                self.assertEqual(repository_update.check()['source'], 'live')
        for corrupt in ('{broken', '[]', json.dumps({**record, 'checked_at': self.clock.return_value + 1})):
            with self.subTest(cache=corrupt):
                self.cache.write_text(corrupt, encoding='utf-8')
                self.assertEqual(repository_update.check()['source'], 'live')

    def test_new_local_commit_invalidates_a_cached_notice(self):
        repository_update.check()
        self.loose_main.write_text(NEW_HEAD + '\n', encoding='ascii')
        self.probe.return_value = {'status': 'up_to_date', 'ahead_by': 0}
        reply, terminal = self.cli()
        self.assertEqual(reply['data']['repository_update']['status'], 'up_to_date')
        self.assertNotIn('[更新提醒]', terminal)
        self.probe.assert_called_with(NEW_HEAD)

    def test_failure_has_one_hour_cache_force_bypasses_it_and_logs_stay_clean(self):
        for error in (subprocess.TimeoutExpired('private-fixture', 2), OSError('private-fixture'),
                      HTTPError('https://example.invalid', 404, 'private-fixture', None, None),
                      HTTPError('https://example.invalid', 429, 'private-fixture', None, None)):
            with self.subTest(error=type(error).__name__):
                self.probe.reset_mock()
                self.probe.side_effect = error
                reply, terminal = self.cli('--check-updates')
                update = reply['data']['repository_update']
                self.assertEqual((update['status'], update['source']), ('unavailable', 'live'))
                self.assertTrue(reply['ok'])
                self.assertNotIn('private-fixture', terminal + str(reply))
                self.clock.return_value += 3599
                self.assertEqual(repository_update.check()['source'], 'cache')
                self.probe.assert_called_once()
                self.clock.return_value += 1
                self.assertEqual(repository_update.check()['source'], 'live')
                self.assertEqual(self.probe.call_count, 2)

    def test_missing_git_checkout_and_non_main_are_quiet(self):
        self.git_available.return_value = None
        self.assertEqual(repository_update.check(force=True)['status'], 'not_checked')
        self.git_available.return_value = 'git'
        (self.git / 'HEAD').write_text('ref: refs/heads/topic\n', encoding='ascii')
        self.assertEqual(repository_update.check(force=True)['status'], 'not_checked')
        (self.git / 'HEAD').write_text(HEAD + '\n', encoding='ascii')
        self.assertEqual(repository_update.check(force=True)['status'], 'not_checked')
        with patch.object(paths, 'PROJECT_ROOT', self.root / 'package'):
            self.assertEqual(repository_update.check(force=True)['status'], 'not_checked')
        self.probe.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_linked_worktree_reads_packed_main_ref_from_common_directory(self):
        self.loose_main.unlink()
        (self.git / 'packed-refs').write_text(f'# Git refs\n{HEAD} refs/heads/main\n', encoding='ascii')
        worktree = self.root / 'worktree'
        worktree.mkdir()
        worktree_meta = self.git / 'worktrees' / 'example'
        worktree_meta.mkdir(parents=True)
        (worktree_meta / 'HEAD').write_text('ref: refs/heads/main\n', encoding='ascii')
        (worktree_meta / 'commondir').write_text('../..\n', encoding='utf-8')
        (worktree / '.git').write_text('gitdir: ../.git/worktrees/example\n', encoding='utf-8')
        with patch.object(paths, 'PROJECT_ROOT', worktree):
            self.assertEqual(repository_update.check()['status'], 'update_available')
        self.probe.assert_called_once_with(HEAD)

    def test_cache_lock_contention_skips_without_running_a_second_probe(self):
        entered, release = Event(), Event()

        def hold_probe(_):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release probe')
            return UPDATE

        self.probe.side_effect = hold_probe
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(repository_update.check)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(repository_update.check()['status'], 'unavailable')
                self.probe.assert_called_once_with(HEAD)
            finally:
                release.set()
            self.assertEqual(first.result()['source'], 'live')
        self.assertEqual(repository_update.check()['source'], 'cache')

    def test_cache_write_failure_and_checkout_change_do_not_print_stale_updates(self):
        with patch.object(repository_update, '_write_cache', side_effect=PermissionError('private-fixture')):
            reply, terminal = self.cli()
            self.assertEqual(reply['data']['repository_update']['status'], 'unavailable')
            self.assertNotIn('private-fixture', terminal + str(reply))

        def change_head(_):
            self.loose_main.write_text(NEW_HEAD + '\n', encoding='ascii')
            return UPDATE

        self.probe.side_effect = change_head
        reply, terminal = self.cli('--check-updates')
        self.assertEqual(reply['data']['repository_update']['status'], 'not_checked')
        self.assertNotIn('[更新提醒]', terminal)


class GithubCompareTest(TestCase):
    def test_compare_is_public_and_distinguishes_fast_forward_from_a_fork(self):
        cases = [('ahead', 2, 0, 'update_available'), ('identical', 0, 0, 'up_to_date'),
                 ('behind', 0, 3, 'local_ahead'), ('diverged', 2, 3, 'diverged')]
        for status, ahead, behind, expected in cases:
            with self.subTest(status=status):
                body = json.dumps({'status': status, 'ahead_by': ahead, 'behind_by': behind}).encode()
                transport = Mock(return_value=io.BytesIO(body))
                self.assertEqual(github_compare.compare(HEAD, open_request=transport),
                                 {'status': expected, 'ahead_by': ahead})
                request, = transport.call_args.args
                self.assertEqual(request.full_url, f'https://api.github.com/repos/wuye999/wb/compare/{HEAD}...main?per_page=1')
                self.assertEqual(request.get_method(), 'GET')
                self.assertFalse(request.has_header('Authorization'))
                self.assertEqual(transport.call_args.kwargs, {'timeout': 1})

    def test_malformed_response_or_transport_failure_returns_no_comparison(self):
        for body in (b'not JSON', b'{"status":"ahead","ahead_by":1,"behind_by":1}',
                     b'{"status":"identical","ahead_by":true,"behind_by":0}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                github_compare.compare(HEAD, open_request=Mock(return_value=io.BytesIO(body)))
        with self.assertRaises(URLError):
            github_compare.compare(HEAD, open_request=Mock(side_effect=URLError('offline')))

    def test_real_probe_boundary_configures_short_timeout_and_does_not_invoke_a_shell(self):
        runner = Mock(return_value=SimpleNamespace(stdout=json.dumps(UPDATE)))
        self.assertEqual(repository_update._probe(HEAD, runner=runner), UPDATE)
        command, = runner.call_args.args
        self.assertEqual(command[1:], ['-I', github_compare.__file__, HEAD])
        self.assertEqual(runner.call_args.kwargs['timeout'], 2)
        self.assertEqual(runner.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertFalse(runner.call_args.kwargs.get('shell', False))
        runner.side_effect = subprocess.TimeoutExpired(command, 2)
        with self.assertRaises(subprocess.TimeoutExpired):
            repository_update._probe(HEAD, runner=runner)

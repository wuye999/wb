"""Offline verification of browser setup, BCS sync/first binding and doctor."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime
import hashlib
import os
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import requests

from catalog_fixture import document, mock_non_git_checkout, snapshot, write_book
from sellerops.cli.main import main
from sellerops.core.errors import AuthenticationError, NetworkError, PlatformApiError
from sellerops.features.catalog import products, shops
from sellerops.features.system import doctor, login
from sellerops.integrations.bcs import client as bcs_client
from sellerops.integrations.bcs.session import BCSSession
from sellerops.integrations.sessions import browser, browser_install
from sellerops.integrations.wildberries import browser_session, session_update
from sellerops.persistence import db
from sellerops.persistence.snapshots import save_shop
from sellerops.settings import paths


SID_A = '00000000-0000-0000-0000-0000000000ae'
SID_B = '00000000-0000-0000-0000-0000000000be'


class BrowserInstallTest(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def test_mirror_fallback_shares_the_remaining_budget_and_stops_on_integrity_failure(self):
        artifact = browser_install.ARTIFACTS[0]
        budgets = {'official': 180., 'npmmirror': 180.}
        replies = [SimpleNamespace(returncode=browser_install._NETWORK), SimpleNamespace(returncode=0)]
        with (patch.object(browser_install.subprocess, 'run', side_effect=replies) as run,
              patch.object(browser_install.time, 'monotonic', side_effect=[0, 15, 15, 20])):
            self.assertEqual(browser_install._download_archive(artifact, self.root, budgets).suffix, '.zip')
        self.assertEqual([c.args[0][-2] for c in run.call_args_list], ['official', 'npmmirror'])
        self.assertEqual(budgets, {'official': 165., 'npmmirror': 175.})
        with patch.object(browser_install.subprocess, 'run', return_value=SimpleNamespace(
                returncode=browser_install._INTEGRITY)) as run:
            with self.assertRaisesRegex(browser_install.InstallError, 'SHA-256'):
                browser_install._download_archive(artifact, self.root, budgets)
        run.assert_called_once()

    def test_download_child_retains_parent_user_site_policy(self):
        artifact = browser_install.ARTIFACTS[0]
        for no_user_site, flags in ((0, ['-E']), (1, ['-E', '-s'])):
            with self.subTest(no_user_site=no_user_site):
                interpreter = SimpleNamespace(executable='fixture-python', flags=SimpleNamespace(no_user_site=no_user_site))
                with (patch.object(browser_install, 'sys', interpreter),
                      patch.dict(browser_install.os.environ, {'PYTHONUSERBASE': str(self.root)}),
                      patch.object(browser_install.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run):
                    browser_install._download_archive(artifact, self.root, {'official': 180.})
                command = run.call_args.args[0]
                self.assertEqual(command, ['fixture-python', *flags, str(Path(browser_install.__file__).resolve()),
                                           artifact.directory, 'official', str(self.root / (artifact.directory + '.zip'))])
                self.assertNotIn('PYTHONUSERBASE', run.call_args.kwargs['env'])

    def test_download_checks_redirect_and_archive_bytes(self):
        payload = b'archive fixture'
        artifact = browser_install.Artifact('fixture', 'browser.exe', 'cft/fixture.zip', len(payload),
                                           hashlib.sha256(payload).hexdigest())
        first, second = artifact.urls('official')
        redirect = SimpleNamespace(status_code=307, headers={'Location': second})
        result = SimpleNamespace(status_code=200, iter_content=lambda **kwargs: [payload])
        for target, outcome in ((second, 0), ('https://example.test/fixture.zip', browser_install._SECURITY)):
            with self.subTest(target=target):
                destination = self.root / ('valid.zip' if not outcome else 'unexpected.zip')
                redirect.headers['Location'] = target
                with patch.object(requests.Session, 'get', side_effect=[nullcontext(redirect), nullcontext(result)]) as send:
                    self.assertEqual(browser_install._download(artifact, 'official', destination), outcome)
                self.assertEqual(send.call_args_list[0].args, (first,))
                self.assertFalse(send.call_args_list[0].kwargs['allow_redirects'])
                self.assertEqual(destination.is_file(), not outcome)
        result.iter_content = lambda **kwargs: [b'x' * len(payload)]
        with patch.object(requests.Session, 'get', return_value=nullcontext(result)):
            self.assertEqual(browser_install._download(artifact, 'npmmirror', self.root / 'changed.zip'),
                             browser_install._INTEGRITY)

    def test_redirect_without_location_is_a_security_failure(self):
        response = requests.Response()
        response.status_code = 307
        destination = self.root / 'unexpected.zip'
        with patch.object(requests.Session, 'get', return_value=nullcontext(response)) as send:
            self.assertEqual(browser_install._download(browser_install.ARTIFACTS[0], 'official', destination),
                             browser_install._SECURITY)
        send.assert_called_once()
        self.assertFalse(destination.exists())

    def test_extract_rejects_unsafe_members_before_writing_outside_staging(self):
        artifact = browser_install.ARTIFACTS[0]
        archive = self.root / 'fixture.zip'
        with ZipFile(archive, 'w') as package:
            package.writestr('../outside.txt', 'fixture')
        with self.assertRaisesRegex(browser_install.InstallError, '不安全'):
            browser_install._extract(archive, self.root / 'stage', artifact)
        self.assertFalse((self.root / 'outside.txt').exists())

    def test_staged_version_failure_preserves_existing_runtime(self):
        runtime = self.root / 'browsers'
        runtime.mkdir()
        (runtime / 'existing').touch()
        artifact = browser_install.ARTIFACTS[0]
        with (patch.object(browser_install, 'stage_install'),
              patch.object(browser, '_driver', side_effect=lambda stage: nullcontext(
                  SimpleNamespace(executable_path=stage / artifact.directory / artifact.executable))),
              patch.object(browser, '_installed_version', return_value='1.0.0.0')):
            with self.assertRaisesRegex(AuthenticationError, browser.CHROMIUM_VERSION):
                browser._setup_verified(runtime, 'official', 180)
        self.assertEqual(list(runtime.iterdir()), [runtime / 'existing'])
        self.assertEqual(list(self.root.iterdir()), [runtime])

    def test_failed_publish_restores_the_previous_runtime(self):
        runtime = self.root / 'browsers'
        runtime.mkdir()
        (runtime / 'existing').touch()
        stage = self.root / 'temporary' / 'staged'
        stage.mkdir(parents=True)
        original_replace = Path.replace

        def replace(path, target):
            if path == stage:
                raise OSError('fixture publish failure')
            return original_replace(path, target)

        with patch.object(Path, 'replace', replace):
            with self.assertRaises(OSError):
                browser._publish_install(stage, runtime)
        self.assertEqual(list(runtime.iterdir()), [runtime / 'existing'])
        self.assertTrue(stage.is_dir())


class OnboardingWorkflowTest(TestCase):
    def setUp(self):
        mock_non_git_checkout(self)
        self.data = Path(self.enterContext(TemporaryDirectory()))
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(self.data)

    def cli(self, *args, use_json=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['--data-dir', str(self.data), *args, *(['--json'] if use_json else [])])
        return code, json.loads(out.getvalue()) if use_json else out.getvalue(), err.getvalue()

    def test_parallel_sync_reports_phases_and_never_reuses_task_id(self):
        client = bcs_client.BCSClient(BCSSession('fixture', 'limit', 'Admin-Token=fixture; Limit-Key=limit'))

        def submit(shop_id):
            if shop_id == 2:
                raise PlatformApiError('private fixture', status_code=503)
            return str(shop_id)

        def poll(task_id, **kwargs):
            if task_id == '3':
                raise NetworkError('private fixture')

        out = io.StringIO()
        with (patch.object(client, 'sync_shop', side_effect=submit) as submitted,
              patch.object(client, 'wait_sync_done', side_effect=poll) as polled, redirect_stdout(out)):
            failures = client.sync_shops_parallel([3, 2, 1])
        self.assertEqual([(f['id'], f['stage']) for f in failures], [(3, 'poll'), (2, 'submit')])
        self.assertEqual(submitted.call_count, 3)
        self.assertEqual({p.args[0] for p in polled.call_args_list}, {'1', '3'})
        self.assertIn('店铺 3 | 轮询进度', out.getvalue())
        self.assertIn('503', out.getvalue())
        self.assertNotIn('private fixture', out.getvalue())
        with (patch.object(client, 'request_json', return_value={'code': 200}) as request,
              patch.object(bcs_client.time, 'time_ns', return_value=1024000000),
              ThreadPoolExecutor(4) as pool):
            ids = list(pool.map(client.sync_shop, range(1, 9)))
        self.assertEqual(len(set(ids)), 8)
        self.assertTrue(all(c.kwargs['mutating'] for c in request.call_args_list))

    def test_sync_fallback_uses_same_shop_baselines_and_preserves_final_error_contract(self):
        snapshot(2, [], fetchMethod='fixture old snapshot')
        original = document(2)
        shops = [{'id': n, 'name': f'Shop {n}'} for n in (1, 2)]
        warnings = [{'id': 1, 'stage': 'submit', 'reason': 'BCS 网络请求失败'},
                    {'id': 2, 'stage': 'poll', 'reason': '等待 BCS 同步进度超时'}]
        with (patch.object(products.bcs, 'fetch_shop_list', return_value=shops),
              patch.object(products.bcs, 'sync_shops_parallel', return_value=warnings),
              patch.object(products.time, 'sleep'),
              patch.object(products.bcs, 'fetch_shop_products', side_effect=[[], NetworkError(), [], []]) as fetch):
            code, reply, output = self.cli('sync', '--all')
            self.assertEqual(code, 1)
            self.assertEqual(reply['data']['sync_warnings'], warnings)
            self.assertEqual([r['id'] for r in reply['data']['errors']], [2])
            self.assertEqual(document(2), original)
            self.assertIn('降级', output)
            code, reply, _ = self.cli('sync', '--all')
            self.assertEqual(code, 0)
            self.assertIn('2 家店铺使用 BCS 缓存', reply['summary'])
        self.assertEqual([(c.args[0], c.kwargs['filter_type']) for c in fetch.call_args_list], [(1, 'BASE'), (2, 'BASE')] * 2)
        with db.connect(read_only=True) as connection:
            self.assertFalse(any(row[0] for row in connection.execute('SELECT last_sync_error FROM shops')))

    def test_bcs_binding_projection_checks_pagination_and_never_exposes_keys(self):
        client = bcs_client.BCSClient(BCSSession('fixture', 'limit', 'Admin-Token=fixture; Limit-Key=limit'))
        first = {'id': 4, 'shopName': 'BCS A', 'sid': SID_A, 'apiKey': 'private fixture'}
        second = {'id': '5', 'shopName': 'BCS B', 'apiKey': 'private fixture'}
        with patch.object(client, 'request_json', side_effect=[{'code': 200, 'total': 2, 'rows': [first]},
                                                              {'code': 200, 'total': 2, 'rows': [second]}]) as request:
            self.assertEqual(client.fetch_binding_shops(), [{'id': 4, 'name': 'BCS A', 'sid': SID_A},
                                                           {'id': 5, 'name': 'BCS B', 'sid': None}])
        self.assertTrue(all(c.args[0] == 'GET' and c.kwargs['retry'] == 1
                            and c.kwargs['allow_redirects'] is False for c in request.call_args_list))
        with patch.object(client, 'request_json', return_value={'code': 200, 'total': 2, 'rows': [first]}):
            with self.assertRaisesRegex(PlatformApiError, '重复'):
                client.fetch_binding_shops()

    def test_wb_preview_only_recommends_unique_sids_and_never_saves(self):
        config = Path(paths.current.credentials_json)
        config.write_text(json.dumps({'bcs': {'token': 'private fixture token', 'limit_key': 'private fixture key'}}),
                          encoding='utf-8')
        original = config.read_bytes()
        wb_shops = (browser_session.WBShop(SID_A, 10, 'WB A'), browser_session.WBShop(SID_B, 20, 'WB B'))
        session = browser_session.WBSession('private fixture wb', '', wb_shops)
        candidates = [{'id': 1, 'name': 'BCS A', 'sid': SID_A.upper()}, {'id': 2, 'name': 'BCS B', 'sid': SID_B},
                      {'id': 3, 'name': 'BCS C', 'sid': SID_B}, {'id': 4, 'name': 'BCS D', 'sid': None}]
        with (patch.object(login, '_browser_session', return_value=session),
              patch.object(login.BCSClient, 'fetch_binding_shops', return_value=candidates) as bcs,
              patch.object(session_update, 'update_shops') as save):
            code, output, _ = self.cli('login', 'wb', use_json=False)
            self.assertEqual(code, 2)
            self.assertIn(f'login wb --resume --bind 1={SID_A}', output)
            self.assertIn('sid 对应多个 BCS 店铺', output)
            self.assertIn('缺少有效 sid', output)
            self.assertNotIn('--bind 2=', output)
            bcs.side_effect = AuthenticationError('private fixture')
            code, output, _ = self.cli('login', 'wb', use_json=False)
            self.assertEqual(code, 2)
            self.assertIn('BCS 会话不可用', output)
            self.assertIn('WB A', output)
        save.assert_not_called()
        self.assertNotIn('private fixture', output)
        self.assertEqual(config.read_bytes(), original)

    def test_doctor_reports_public_summaries_without_migrating_old_database(self):
        config = Path(paths.current.credentials_json)
        config.write_text(json.dumps({'bcs': {'token': 'private fixture', 'limit_key': 'secret'},
                                      'wb': {'shops': [{'shopId': 1, 'authorizev3': 'secret', 'wb_seller_lk': 'secret',
                                                       'cookie': 'private fixture'}, {'shopId': 2}]},
                                      'feishu': {'base_url': 'https://example.test/private-fixture'}}), encoding='utf-8-sig')
        snapshot(1, [], fetchedAt='2026-01-01 10:00:00')
        snapshot(3, [], fetchedAt='2026-01-01 10:00:00')
        with db.connect(write=True) as connection:
            connection.execute("UPDATE shops SET status='archived' WHERE shop_id='3'")
            connection.execute('ALTER TABLE products DROP COLUMN shipping_mode')
        write_book([('DUP', '甲', None, '', 'fixture-stock', 'WB发货'), ('DUP', '乙', '无效', '', '', '马帮发货'),
                    ('', '乙', '=1+2', '', '', '不处理'), ('', '', 12, '', '', '错'), (None,) * 6])
        files = [config, Path(paths.current.database), Path(paths.current.price_book_xlsx)]
        before = [file.read_bytes() for file in files]
        with (patch.object(doctor, 'online_checks', side_effect=AssertionError('unexpected network')),
              patch.object(browser, '_driver', side_effect=AssertionError('unexpected browser'))):
            code, reply, terminal = self.cli('doctor')
        self.assertEqual(code, 0)
        body = reply['data']
        self.assertEqual(body['credentials']['status'], 'ready')
        self.assertEqual(body['shop_counts'], {'credential_configured': 2, 'credential_session_complete': 1,
                                               'local_synced': 2, 'local_synced_active': 1, 'local_synced_archived': 1})
        self.assertEqual(body['price_book']['data_rows'], 4)
        self.assertEqual(body['price_book']['issue_counts'], {
            'missing_price': 1, 'invalid_price': 1, 'wb_shipping_with_mabang_sku': 1,
            'mabang_shipping_without_stock_sku': 1, 'price_formula_without_cache': 1,
            'missing_product_identity': 1, 'invalid_shipping_mode': 1, 'duplicate_seller_sku': 2,
            'ambiguous_product_name': 1,
        })
        self.assertIsNone(body['browser']['chromium']['runtime_version'])
        self.assertIn('本地已同步 2 家', terminal)
        self.assertNotIn('private fixture', json.dumps(reply) + terminal)
        self.assertNotIn('private-fixture', json.dumps(reply) + terminal)
        self.assertEqual([file.read_bytes() for file in files], before)

    def test_snapshot_freshness_uses_age_and_selected_active_stores(self):
        now = datetime.fromisoformat('2026-11-26T06:00:00+08:00').timestamp()
        snapshot(1, [], fetchedAt='2026-11-25T06:00:00+08:00')
        snapshot(2, [], fetchedAt='2026-11-25T06:00:01+08:00')
        snapshot(3, [], fetchedAt='2026-11-24')
        snapshot(4, [], fetchedAt='2026-11-26T16:00:00+08:00')
        snapshot(5, [], fetchedAt='invalid')
        snapshot(6, [], fetchedAt='2026-11-20T13:00:00+08:00')
        snapshot(8, [])
        with db.connect(write=True) as connection:
            connection.execute("UPDATE shops SET last_sync_error='last fetch failed' WHERE shop_id='1'")
            connection.execute("UPDATE shops SET status='archived' WHERE shop_id='6'")
            save_shop(connection, 7)

        with (patch.object(shops, 'time', return_value=now),
              patch.dict(os.environ, {'SELLEROPS_SNAPSHOT_MAX_AGE_HOURS': '0'}),
              patch.object(products, 'fetch_all', side_effect=AssertionError('unexpected sync')) as fetch):
            code, reply, stderr = self.cli('doctor')
            freshness = reply['data']['snapshot_freshness']
            self.assertEqual((code, freshness['status'], freshness['max_age_hours'], freshness['active_shop_count']),
                             (0, 'checked', 24, 7))
            self.assertEqual({row['shop_id']: row['status'] for row in freshness['shops']},
                             {'1': 'stale', '3': 'stale', '4': 'unknown_age', '5': 'unknown_age',
                              '8': 'unknown_age', '7': 'never_synced'})
            self.assertEqual(freshness['shops'][0]['age_hours'], 24)
            self.assertEqual(reply['data']['shops'][0]['last_sync_error'], 'last fetch failed')
            self.assertNotIn('snapshot_present', reply['data']['shops'][0])
            self.assertIn('[快照提醒]', stderr)

            code, reply, stderr = self.cli('catalog', 'product-summary', '--shops', '1,2,7')
            freshness = reply['data']['snapshot_freshness']
            self.assertEqual((code, freshness['active_shop_count']), (0, 3))
            self.assertEqual([row['shop_id'] for row in freshness['shops']], ['1', '7'])
            self.assertIn('[快照提醒]', stderr)

            code, reply, stderr = self.cli('shop', 'list', '--name', 'shop6')
            self.assertEqual((code, reply['data']['snapshot_freshness']['active_shop_count']), (0, 0))
            self.assertNotIn('[快照提醒]', stderr)

            with patch.dict(os.environ, {'SELLEROPS_SNAPSHOT_MAX_AGE_HOURS': '48'}):
                code, reply, stderr = self.cli('shop', 'list', '--name', 'shop1')
                freshness = reply['data']['snapshot_freshness']
                self.assertEqual((code, freshness['max_age_hours'], freshness['shops']), (0, 48, []))
                self.assertNotIn('[快照提醒]', stderr)

            code, terminal, _ = self.cli('shop', 'list', '--name', 'shop7', use_json=False)
            self.assertEqual(code, 0)
            self.assertIn('sync --shop-id 7', terminal)
            fetch.assert_not_called()

    def test_doctor_distinguishes_invalid_json_and_invalid_structure(self):
        config = Path(paths.current.credentials_json)
        for text, status in (('{"secret":', 'invalid_json'), ('{"secret":NaN}', 'invalid_json'),
                             ('[]', 'invalid_structure'),
                             ('{"wb":{"shops":[{"shopId":1},{"shopId":1},null]}}', 'invalid_structure')):
            with self.subTest(text=text):
                config.write_text(text, encoding='utf-8')
                code, reply, output = self.cli('doctor')
                self.assertEqual(code, 0)
                self.assertEqual(reply['data']['credentials']['status'], status)
                self.assertNotIn('secret', json.dumps(reply) + output)

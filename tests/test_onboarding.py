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
from sellerops.features.system import doctor, login, online_auth
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

        def submit(shop_id, **kwargs):
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
        self.assertIn('本地有快照 2 家', terminal)
        self.assertNotIn('private fixture', json.dumps(reply) + terminal)
        self.assertNotIn('private-fixture', json.dumps(reply) + terminal)
        self.assertEqual([file.read_bytes() for file in files], before)

    def test_snapshot_freshness_uses_age_and_selected_active_stores(self):
        now = datetime.fromisoformat('2026-11-26T06:00:00+08:00').timestamp()
        for sid, stamp in enumerate(('2026-11-25T06:00:00+08:00', '2026-11-25T06:00:01+08:00', '2026-11-24',
                                     '2026-11-26T16:00:00+08:00', 'invalid', '2026-11-20T13:00:00+08:00'), 1):
            snapshot(sid, [], fetchedAt=stamp, source='bcs', snapshot_status='bcs_synced')
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

    def test_doctor_cold_start_prioritizes_credentials_before_sync(self):
        write_book([('DEMO', '样例', 12, 'DEMO', '', 'WB发货')])
        config = Path(paths.current.credentials_json)
        with patch.object(doctor, 'online_checks') as probe:
            code, reply, terminal = self.cli('doctor', '--check-online')
        probe.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(reply['data']['credentials']['status'], 'missing')
        self.assertFalse(reply['data']['credentials_file_exists'])
        self.assertEqual(reply['data']['online'], {})
        self.assertIn('[在线鉴权] 未运行，缺少凭证文件', terminal)
        self.assertIn('examples/credentials.example.json', reply['next_action'])
        self.assertFalse(config.exists())

        template = (paths.PROJECT_ROOT / 'examples' / 'credentials.example.json').read_text(encoding='utf-8')
        for payload in ('{}', template):
            with self.subTest(template=payload == template):
                config.write_text(payload, encoding='utf-8')
                code, reply, terminal = self.cli('doctor')
                self.assertEqual(code, 0)
                data = reply['data']
                self.assertEqual(data['price_book']['status'], 'ready')
                self.assertEqual(data['credentials']['status'], 'unconfigured')
                self.assertTrue(data['credentials']['json_valid'])
                self.assertTrue(data['credentials']['structure_valid'])
                self.assertEqual(data['credentials']['warnings'], [])
                self.assertTrue(all(not platform['configured'] for platform in data['platforms'].values()))
                self.assertEqual(data['shop_counts']['credential_configured'], 0)
                self.assertEqual(data['platforms']['mabang']['mapped_shops'], 0)
                self.assertIn('填写', reply['next_action'])
                self.assertNotIn('sync', reply['next_action'])
                self.assertNotIn('[在线鉴权]', terminal)
                if payload == template:
                    self.assertTrue(data['platforms']['bcs']['auto_refresh'])
                    self.assertTrue(data['platforms']['mabang']['login']['auto_refresh'])

        config.write_text(json.dumps({'feishu': {'base_url': 'https://fixture.invalid/bitable'}}), encoding='utf-8')
        _, reply, _ = self.cli('doctor')
        self.assertNotIn('sync', reply['next_action'])
        config.write_text('{"bcs":{"token":"fixture-token","limit_key":"fixture-limit"}}', encoding='utf-8')
        _, reply, _ = self.cli('doctor')
        self.assertIn('sync', reply['next_action'])

    def test_doctor_warns_about_legacy_sample_entries_without_counting_them(self):
        config = Path(paths.current.credentials_json)
        old_shop = {'shopId': 1, 'shopName': '示例店铺', 'authorizev3': '', 'wb_seller_lk': '', 'cookie': ''}
        data = {'wb': {'shops': [old_shop]}, 'mabang': {'shop_map': {'马帮中的店铺标识': '示例店铺(1)'}}}
        config.write_text(json.dumps(data), encoding='utf-8')
        original = config.read_bytes()
        _, reply, terminal = self.cli('doctor')
        credentials = reply['data']['credentials']
        self.assertEqual(credentials['status'], 'unconfigured')
        self.assertTrue(credentials['structure_valid'])
        self.assertEqual(credentials['warnings'], [
            {'path': 'wb.shops', 'code': 'legacy_wb_template_shop', 'shop_ids': [1]},
            {'path': 'mabang.shop_map', 'code': 'legacy_mabang_template_shop_map'},
        ])
        self.assertIn('移除旧模板', reply['next_action'])
        self.assertIn('旧模板的示例店铺', terminal)
        self.assertIn('旧模板的示例映射', terminal)
        self.assertEqual(config.read_bytes(), original)

        data['wb']['shops'] += [{'shopId': 2, 'shopName': '真实店铺'}, {'shopId': 3, 'shopName': '示例店铺'}]
        data['mabang']['shop_map']['real-fixture'] = '真实店铺(2)'
        config.write_text(json.dumps(data), encoding='utf-8')
        _, reply, _ = self.cli('doctor')
        self.assertEqual(reply['data']['credentials']['status'], 'ready')
        self.assertEqual(reply['data']['shop_counts']['credential_configured'], 2)
        self.assertEqual(reply['data']['platforms']['mabang']['mapped_shops'], 1)

    def test_doctor_reports_password_presence_and_effective_refresh(self):
        config = Path(paths.current.credentials_json)
        username = 'account-fixture-private'
        password = 'password-fixture-private'
        settings = {'bcs': {'login': {'username': username}}, 'mabang': {'login': {'username': username}}}
        config.write_text(json.dumps(settings), encoding='utf-8')
        runtime = {**browser.installation_status(), 'status': 'ready'}
        with patch.object(browser, 'installation_status', return_value=runtime):
            code, reply, terminal = self.cli('doctor')
        self.assertEqual(code, 0)
        bcs, mabang = (reply['data']['platforms'][key] for key in ('bcs', 'mabang'))
        self.assertTrue(bcs['login_configured'])
        self.assertFalse(bcs['password_configured'])
        self.assertTrue(bcs['auto_refresh'])
        self.assertFalse(bcs['auto_refresh_enabled'])
        self.assertEqual(mabang['login'], {'username_configured': True, 'password_configured': False,
                                          'auto_refresh': False, 'auto_refresh_enabled': False,
                                          'session_configured': False})
        self.assertIn('login bcs，', reply['next_action'])
        self.assertIn('密码未填', terminal)
        self.assertIn('可使用浏览器登录', terminal)
        for key, enabled, endpoint in ((None, True, None), (False, False, None), (True, True, None),
                                       (True, False, 'https://private-endpoint.invalid')):
            with self.subTest(value=key, endpoint=endpoint):
                login_section = {'username': username, 'password': password}
                if key is not None:
                    login_section['auto_refresh'] = key
                settings = {'bcs': {'login': login_section}, 'mabang': {'login': login_section}}
                if endpoint:
                    settings['bcs']['base_url'] = endpoint
                config.write_text(json.dumps(settings), encoding='utf-8')
                _, reply, terminal = self.cli('doctor')
                bcs, mabang = (reply['data']['platforms'][key] for key in ('bcs', 'mabang'))
                self.assertTrue(bcs['password_configured'])
                self.assertTrue(mabang['login']['password_configured'])
                self.assertIs(bcs['auto_refresh_enabled'], enabled)
                self.assertIs(mabang['login']['auto_refresh_enabled'], key is True)
                self.assertIn('密码已填', terminal)
                public = json.dumps(reply) + terminal
                for secret in (username, password, 'account-fixture', 'password-fixture', 'private-endpoint'):
                    self.assertNotIn(secret, public)

    def test_doctor_separates_mabang_fulfillment_from_login(self):
        config = Path(paths.current.credentials_json)
        for fields, handover in (({'handover_channel_id': 'private-channel-id'}, False),
                                 ({'handover_channel_value': 'private-channel-fallback'}, True),
                                 ({'handover_keyword': 'private-channel-keyword'}, True)):
            with self.subTest(fields=fields):
                settings = {'login': {'username': 'private-mabang-user', 'password': 'private-mabang-password'},
                            'warehouse_id': 42, 'shop_map': {'private-mabang-store': 'fixture(2)'}, **fields}
                config.write_text(json.dumps({'mabang': settings}), encoding='utf-8')
                _, reply, terminal = self.cli('doctor')
                mabang = reply['data']['platforms']['mabang']
                self.assertEqual(mabang['fulfillment'], {'shop_map_configured': True, 'mapped_shops': 1,
                                                         'warehouse_configured': True, 'warehouse_mode': 'id',
                                                         'handover_configured': handover})
                self.assertFalse(mabang['session_configured'])
                self.assertIn('login mabang', reply['next_action'])
                self.assertNotIn('login setup', reply['next_action'])
                self.assertIn('[马帮登录]', terminal)
                self.assertIn('[马帮订单履约]', terminal)
                self.assertNotIn('private-', json.dumps(reply) + terminal)

    def test_doctor_online_summary_and_read_only_bcs_probe(self):
        config = Path(paths.current.credentials_json)
        config.write_text(json.dumps({
            'bcs': {'login': {'username': 'private-account', 'password': 'private-password', 'auto_refresh': True},
                    'token': 'private-bcs-token', 'limit_key': 'private-bcs-key'},
            'wb': {'shops': [{'shopId': 12, 'authorizev3': 'private-authorize',
                             'wb_seller_lk': 'private-seller-lk', 'cookie': 'private-wb-cookie'}]},
            'feishu': {'base_url': 'https://fixture.invalid/private-feishu-url'},
        }), encoding='utf-8')
        before = config.read_bytes()
        rejected = requests.Response()
        rejected.status_code = 401
        rejected._content = b'private-platform-response'
        with (patch.object(requests, 'request', return_value=rejected) as request,
              patch.object(bcs_client, 'refresh_login') as refresh,
              patch.object(online_auth, 'probe_session') as wb,
              patch.object(online_auth.tables, 'resolve_base', side_effect=RuntimeError('private-feishu-error'))):
            code, reply, terminal = self.cli('doctor', '--check-online')
            _, text_output, _ = self.cli('doctor', '--check-online', use_json=False)
        self.assertEqual(code, 0)
        self.assertTrue(reply['data']['online_checked'])
        self.assertEqual(reply['data']['online'], {'bcs': 'authentication_failed', 'wb': {'12': 'verified'},
                                                   'mabang': 'missing', 'feishu': 'unavailable'})
        self.assertIn('login bcs', reply['next_action'])
        self.assertEqual(request.call_count, 2)
        for call in request.call_args_list:
            self.assertEqual(call.args[0], 'GET')
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(wb.call_count, 2)
        refresh.assert_not_called()
        expected_lines = ('BCS 鉴权失败', 'WB 店铺 12 通过', '马帮 缺少会话或配置', '飞书 检查不可用')
        for summary in (terminal, text_output):
            for line in expected_lines:
                self.assertIn('[在线鉴权] ' + line, summary)
            self.assertIn('自动续期开启', summary)
        self.assertNotIn('private-', json.dumps(reply) + terminal + text_output)
        self.assertEqual(config.read_bytes(), before)

    def test_doctor_online_custom_bcs_and_invalid_json_do_not_probe(self):
        config = Path(paths.current.credentials_json)
        config.write_text('{"bcs":{"base_url":"https://fixture.invalid/private-endpoint","token":"private-token",'
                          '"limit_key":"private-key"}}', encoding='utf-8')
        with patch.object(requests, 'request') as request:
            _, reply, terminal = self.cli('doctor', '--check-online')
        request.assert_not_called()
        self.assertEqual(reply['data']['online']['bcs'], 'unavailable')
        self.assertIn('自定义端点未执行探针', terminal)
        self.assertNotIn('private-', json.dumps(reply) + terminal)

        config.write_text('{"private-invalid":NaN}', encoding='utf-8')
        with patch.object(doctor, 'online_checks') as checks:
            _, reply, terminal = self.cli('doctor', '--check-online')
        checks.assert_not_called()
        self.assertEqual(reply['data']['online'], {'credentials': 'unreadable'})
        self.assertIn('[在线鉴权] 未运行', terminal)
        self.assertNotIn('private-', json.dumps(reply) + terminal)

        config.write_text('{}', encoding='utf-8')
        find_spec = doctor.importlib.util.find_spec
        with (patch.object(doctor.importlib.util, 'find_spec',
                           side_effect=lambda name: None if name == 'requests' else find_spec(name)),
              patch.object(doctor, 'online_checks') as checks):
            code, reply, terminal = self.cli('doctor', '--check-online')
        checks.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(reply['data']['online'], {})
        self.assertIn('[在线鉴权] 未运行，缺少 requests 依赖', terminal)

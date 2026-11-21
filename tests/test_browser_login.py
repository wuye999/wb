import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sellerops.cli.main import main
from sellerops.core.errors import AuthenticationError, StorageLockError, ValidationError
from sellerops.core.files import FileLock
from sellerops.features import login
from sellerops.integrations.bcs import auth as bcs
from sellerops.integrations.bcs.session import login_session
from sellerops.integrations.mabang import auth as mabang
from sellerops.integrations.mabang.session import MabangAuthError
from sellerops.integrations.sessions import browser
from sellerops.integrations.wildberries import browser_session as wb_browser
from sellerops.integrations.wildberries import session_update
from sellerops.settings import paths


SHOP_ONE = '00000000-0000-0000-0000-000000000011'
SHOP_TWO = '00000000-0000-0000-0000-000000000022'


def token(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'fixture.{encoded}.signature'


class LoginTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.addCleanup(paths.configure, paths.DATA_DIR)
        paths.configure(directory)
        self.path = Path(paths.CREDENTIALS_JSON)
        self.platforms = {
            'bcs': {'login': {'username': 'fixture', 'password': 'fixture-password'}},
            'mabang': {'login': {'username': 'fixture', 'password': 'fixture-password'}},
        }
        self.path.write_text(json.dumps(self.platforms), encoding='utf-8')
        self.output = self.enterContext(redirect_stdout(io.StringIO()))

    def execute(self, *args):
        return main(['--data-dir', paths.DATA_DIR, 'login', *args])

    def test_password_success_does_not_open_browser(self):
        with patch.object(bcs, 'refresh_login') as direct, patch.object(login, '_browser_session') as window:
            self.assertEqual(self.execute('bcs'), 0)
        direct.assert_called_once()
        window.assert_not_called()

    def test_bcs_verification_stores_validated_browser_session(self):
        session = login_session('fixture-token', 'fixture-limit', {'Gateway': 'fixture'})
        with (patch.object(bcs, 'refresh_login', side_effect=bcs.BrowserVerificationRequired('fixture')),
              patch.object(login, '_browser_session', return_value=session) as window,
              patch.object(bcs, 'verify_account') as verify):
            self.assertEqual(self.execute('bcs'), 0)
        window.assert_called_once_with('bcs', resume=False, keep_open=False)
        verify.assert_called_once_with(session, 'fixture')
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['bcs']['limit_key'], 'fixture-limit')
        self.assertEqual(saved['mabang'], self.platforms['mabang'])
        self.assertNotIn('fixture-token', self.output.getvalue())

    def test_generic_auth_error_does_not_open_browser(self):
        with (patch.object(bcs, 'refresh_login', side_effect=AuthenticationError('fixture')),
              patch.object(login, '_browser_session') as window):
            self.assertEqual(self.execute('bcs'), 1)
        window.assert_not_called()

    def test_wrong_bcs_browser_account_preserves_existing_file(self):
        previous = self.path.read_bytes()
        session = login_session('fixture-token', 'fixture-limit', {})
        with (patch.object(login, '_browser_session', return_value=session),
              patch.object(bcs, 'verify_account', side_effect=AuthenticationError('账号归属不一致'))):
            self.assertEqual(self.execute('bcs', '--resume'), 1)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_explicit_reopening_waits_for_window_before_probing(self):
        events = []

        @contextmanager
        def open_profile(platform):
            events.append('open')
            yield SimpleNamespace(pages=[Mock()])
            events.append('close')

        def probe(_):
            events.append('probe')
            return 'session'

        with (patch.object(browser, 'open_profile', side_effect=open_profile),
              patch.object(browser, 'wait_for_window_close',
                           side_effect=lambda _, timeout_ms: events.append(('wait', timeout_ms))),
              patch.object(wb_browser, 'read_session', side_effect=probe)):
            self.assertEqual(login._browser_session('wb', resume=False, keep_open=False), 'session')
        self.assertEqual(events, ['open', ('wait', 1_200_000), 'close', 'open', 'probe', 'close'])

    def test_resume_missing_profile_leaves_browser_closed(self):
        with patch.object(browser, 'open_profile') as window:
            self.assertEqual(self.execute('wb', '--resume'), 1)
        window.assert_not_called()

    def test_expired_wb_profile_prompts_and_reopens_the_same_profile(self):
        browser.profile_dir('wb').mkdir(parents=True)
        context = SimpleNamespace(pages=[Mock()])
        session = wb_browser.WBSession(token({'user': 'fixture'}), '', ())
        with (patch.object(browser, 'open_profile', side_effect=lambda _: nullcontext(context)) as opened,
              patch.object(browser, 'wait_for_window_close') as wait_for_user,
              patch.object(wb_browser, 'read_session', side_effect=[None, session])):
            self.assertEqual(self.execute('wb'), 0)
        self.assertEqual([call.args for call in opened.call_args_list], [('wb',)] * 3)
        wait_for_user.assert_called_once_with(context, timeout_ms=1_200_000)
        self.assertIn('请在WB官网完成登录和验证', self.output.getvalue())

    def test_project_browser_runtime_is_independent_of_selected_data_directory(self):
        self.assertEqual(paths.BROWSER_RUNTIME_DIR, paths.PROJECT_ROOT / '.runtime' / 'browsers')
        self.assertEqual(browser.profile_dir('wb'), Path(paths.DATA_DIR) / 'browser_profiles' / 'wb')
        self.assertEqual(browser.profile_dir('mabang'), Path(paths.DATA_DIR) / 'browser_profiles' / 'mabang')

    def test_profile_in_use_fails_before_opening_another_window(self):
        with (FileLock(browser.profile_dir('wb').with_suffix('.browser')),
              ThreadPoolExecutor() as pool, patch.object(browser, 'open_profile') as window):
            result = pool.submit(login._browser_session, 'wb', resume=True, keep_open=False)
            with self.assertRaises(StorageLockError):
                result.result()
        window.assert_not_called()

    def test_mabang_wrong_browser_identity_keeps_credentials(self):
        previous = self.path.read_bytes()
        with patch.object(mabang, 'request') as send:
            with self.assertRaisesRegex(MabangAuthError, '账号'):
                mabang.connect_browser_session(self.platforms['mabang'], 'other-fixture', [])
        send.assert_not_called()
        self.assertEqual(self.path.read_bytes(), previous)

    def test_browser_wrong_version_fails_closed(self):
        with self.assertRaisesRegex(AuthenticationError, browser.CHROMIUM_VERSION):
            browser._check_version('1.0.0.0')

    def test_wb_empty_account_does_not_change_credentials(self):
        previous = self.path.read_bytes()
        session = wb_browser.WBSession(token({'user': 'fixture'}), '', ())
        with patch.object(login, '_browser_session', return_value=session):
            self.assertEqual(self.execute('wb'), 0)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertIn('暂无店铺', self.output.getvalue())

    def test_wb_empty_browser_list_accepts_placeholder_shop_cookie(self):
        request = Mock(method='POST', url=wb_browser.SUPPLIERS_URL,
                       post_data_json={'id': 1, 'method': 'getUserSuppliers'})
        headers = {'authorizev3': token({'user': 'fixture'}),
                   'cookie': 'x-supplier-id-external=placeholder'}
        request.header_value.side_effect = headers.get
        response = Mock(request=request, url=wb_browser.SUPPLIERS_URL)
        response.json.return_value = {'id': 1, 'result': {'suppliers': []}}
        page = Mock(url=wb_browser.HOME)
        page.expect_response.return_value = nullcontext(SimpleNamespace(value=response))
        context = SimpleNamespace(pages=[page])
        with patch.dict('sys.modules', {'playwright.sync_api': SimpleNamespace(TimeoutError=TimeoutError)}):
            session = wb_browser.read_session(context)
        self.assertIsNotNone(session)
        self.assertEqual(session.shops, ())
        self.assertEqual(session.cookie, '')
        page.goto.assert_called_once_with(wb_browser.HOME, wait_until='commit', timeout=180_000)
        self.assertEqual(page.expect_response.call_args.kwargs['timeout'], 240_000)
        request.header_value.assert_called_once_with('authorizev3')

    def test_wb_expired_profile_returns_on_top_level_auth_redirect(self):
        page = Mock(url=wb_browser.HOME)
        response = Mock(url=wb_browser.LOGIN_ENTRY, request=Mock(resource_type='document'))

        def expect_response(predicate, *, timeout):
            response.frame = Mock()
            self.assertFalse(predicate(response))
            response.frame = page.main_frame
            self.assertTrue(predicate(response))
            return nullcontext(SimpleNamespace(value=response))

        page.expect_response.side_effect = expect_response
        with patch.dict('sys.modules', {'playwright.sync_api': SimpleNamespace(TimeoutError=TimeoutError)}):
            self.assertIsNone(wb_browser.read_session(SimpleNamespace(pages=[page])))
        response.request.header_value.assert_not_called()

    def test_wb_only_saves_confirmed_binding_and_preserves_other_platforms(self):
        authorize = token({'user': 'fixture'})
        shops = (wb_browser.WBShop(SHOP_ONE, 100, 'selected'),
                 wb_browser.WBShop(SHOP_TWO, 200, 'unselected'))
        session = wb_browser.WBSession(authorize, f'x-supplier-id-external={SHOP_ONE}', shops)
        with (patch.object(login, '_browser_session', return_value=session),
              patch.object(login.BCSClient, 'fetch_open_api_shops', return_value=[SimpleNamespace(id=12, name='BCS')]),
              patch.object(session_update.auth, 'sign_seller_token', return_value=token({'data': {'Z-Sid': SHOP_TWO}})),
              patch.object(session_update.auth, 'probe_session') as verify):
            self.assertEqual(self.execute('wb'), 2)
            verify.assert_not_called()
            self.assertEqual(self.execute('wb', '--bind', f'12={SHOP_TWO}'), 0)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data['bcs'], self.platforms['bcs'])
        self.assertEqual([row['shopId'] for row in data['wb']['shops']], [12])
        self.assertIn(SHOP_TWO, data['wb']['shops'][0]['cookie'])
        self.assertNotIn(authorize, self.output.getvalue())

    def test_wb_signing_failure_does_not_store_partial_update(self):
        previous = self.path.read_bytes()
        rows = [{'shopId': 1, 'cookie': f'x-supplier-id-external={SHOP_ONE}'},
                {'shopId': 2, 'cookie': f'x-supplier-id-external={SHOP_TWO}'}]
        with (patch.object(session_update.auth, 'sign_seller_token',
                           side_effect=[token({'data': {'Z-Sid': SHOP_ONE}}), AuthenticationError('fixture')]),
              patch.object(session_update.auth, 'probe_session')):
            with self.assertRaises(ValidationError):
                session_update.update_shops(self.path, {}, rows, token({'user': 'fixture'}), rows[0]['cookie'])
        self.assertEqual(self.path.read_bytes(), previous)

    def test_wb_extracts_supplier_response_by_id_and_rejects_duplicate_shops(self):
        shop = {'id': SHOP_ONE, 'oldID': 1, 'name': 'fixture'}
        payload = [{'id': 2, 'result': {'countries': []}}, {'id': 1, 'result': {'suppliers': [shop]}}]
        self.assertEqual(wb_browser.parse_wb_shops(payload, 1)[0].uuid, SHOP_ONE)
        payload[1]['result']['suppliers'].append(shop)
        with self.assertRaisesRegex(AuthenticationError, '店铺身份'):
            wb_browser.parse_wb_shops(payload, 1)

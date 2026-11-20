"""Offline BCS login fixtures and automatic renewal behavior."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import requests

from sellerops.cli.binding import dispatch
from sellerops.cli.main import build_parser
from sellerops.core.errors import AuthenticationError, RateLimitError
from sellerops.core.files import atomic_dump_json, safe_load_json
from sellerops.integrations.bcs import auth
from sellerops.integrations.bcs.capture import BCS_BASE_URL, LOGIN_URL, BCSSession
from sellerops.integrations.bcs.client import BCSClient
from sellerops.settings import credentials, paths


USERNAME = 'fixture-account'
PASSWORD = 'fixture-password'
SHOP_LIST = BCS_BASE_URL + '/system/wbShop/user/list'
NEW_SESSION = BCSSession('new-token', 'new-limit', 'Admin-Token=new-token; Limit-Key=new-limit; acw_tc=new-cookie')


def reply(status=200, **payload):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode('utf-8')
    return response


def login_har(*, token='new-token', account=USERNAME, limit='new-limit'):
    return {'log': {'entries': [
        {'request': {'method': 'POST', 'url': LOGIN_URL, 'postData': {'text': json.dumps({
            'username': account, 'password': 'har-only-password',
        })}}, 'response': {'status': 200, 'content': {'text': json.dumps({
            'code': 200, 'token': 'new-token', 'limitKey': 'new-limit',
        })}}},
        {'request': {'method': 'GET', 'url': SHOP_LIST + '?pageSize=1', 'headers': [
            {'name': 'authorization', 'value': 'Bearer ' + token},
            {'name': 'x-limit-key', 'value': limit},
            {'name': 'cookie', 'value': f'Admin-Token={token}; Limit-Key={limit}; '
             'acw_tc=new-cookie; password=har-only-password; username=har-name; rememberMe=true'},
        ]}, 'response': {'status': 200}},
    ]}}


class TestPasswordLogin(TestCase):
    def setUp(self):
        self.business = self.enterContext(requests.Session())
        self.login = self.enterContext(requests.Session())
        self.business.cookies.set('acw_tc', 'new-cookie', domain='wb.bcserp.com', path='/')
        self.business.cookies.set('acw_tc', 'login-only', domain='bcserp.com', path='/login_v1')
        self.business_get = self.enterContext(patch.object(self.business, 'get', return_value=reply()))
        self.login_get = self.enterContext(patch.object(self.login, 'get'))
        self.login_post = self.enterContext(patch.object(self.login, 'post', return_value=reply(
            code=200, token='new-token', limitKey='new-limit')))
        self.enterContext(patch.object(auth.requests, 'Session', side_effect=[self.business, self.login]))

    def test_password_submission_requires_explicit_no_slider(self):
        self.login_get.side_effect = [reply(), reply(code=200, needSlider=False)]
        result = auth.password_login(USERNAME, PASSWORD)
        self.assertEqual(result, NEW_SESSION)
        self.assertEqual(self.business_get.call_args.args, (auth.BUSINESS_HOME,))
        self.assertEqual(self.login_get.call_args_list[0].args, (auth.LOGIN_PAGE,))
        self.assertEqual(self.login_get.call_args.args, (LOGIN_URL + '/slider-required',))
        self.assertEqual(self.login_post.call_args.kwargs['json'], {
            'username': USERNAME, 'password': PASSWORD, 'captchaVerifyParam': '',
        })
        self.assertFalse(self.login_post.call_args.kwargs['allow_redirects'])

    def test_slider_stops_before_password_submission(self):
        self.login_get.side_effect = [reply(), reply(code=200, needSlider=True)]
        with self.assertRaisesRegex(AuthenticationError, 'bcs-login --from-har'):
            auth.password_login(USERNAME, PASSWORD)
        self.login_post.assert_not_called()

    def test_gateway_challenge_stops_before_password_submission(self):
        self.login_get.side_effect = [reply(), reply(status=405, msg=PASSWORD)]
        with self.assertRaisesRegex(AuthenticationError, '网关校验') as error:
            auth.password_login(USERNAME, PASSWORD)
        self.assertNotIn(PASSWORD, str(error.exception))
        self.login_post.assert_not_called()


class TestBCSRefresh(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.addCleanup(paths.configure, paths.DATA_DIR)
        paths.configure(directory)
        self.path = Path(paths.CREDENTIALS_JSON)
        self.original = {'bcs': {'token': 'old-token', 'limit_key': 'old-limit', 'cookie_extra': 'acw_tc=old',
                                 'login': {'username': USERNAME, 'password': PASSWORD}},
                         'wb': {'shops': []}, 'mabang': {'unrelated': 'preserve'}}
        atomic_dump_json(self.path, self.original)
        credentials.reload()
        self.password_login = self.enterContext(patch.object(auth, 'password_login', return_value=NEW_SESSION))
        self.verify = self.enterContext(patch.object(auth.requests, 'get', return_value=reply(
            code=200, data={'userName': USERNAME})))

    def test_auto_refresh_retries_rejected_read_once_with_new_headers(self):
        client = BCSClient()
        with patch('requests.request', side_effect=[reply(code=401), reply(code=200, rows=[])]) as send:
            self.assertEqual(client.fetch_shop_list(), [])
        self.password_login.assert_called_once_with(USERNAME, PASSWORD)
        self.assertEqual([call.kwargs['headers']['Authorization'] for call in send.call_args_list],
                         ['Bearer old-token', 'Bearer new-token'])
        self.assertEqual(self.verify.call_args.args, (auth.PROFILE_URL,))
        self.assertEqual(safe_load_json(self.path), {
            **self.original, 'bcs': {**self.original['bcs'], 'token': 'new-token',
                                     'limit_key': 'new-limit', 'cookie_extra': 'acw_tc=new-cookie'},
        })

    def test_retry_failing_authentication_again_is_not_replayed(self):
        with patch('requests.request', return_value=reply(status=401)) as send:
            with self.assertRaises(AuthenticationError):
                BCSClient().get(SHOP_LIST)
        self.assertEqual(send.call_count, 2)
        self.password_login.assert_called_once()

    def test_post_and_get_sync_are_never_reissued_after_renewal(self):
        client = BCSClient()
        for call in (lambda: client.post(BCS_BASE_URL + '/shopKeeper/price/batch', []), lambda: client.sync_shop(1)):
            with self.subTest(call=call):
                atomic_dump_json(self.path, self.original)
                credentials.reload()
                with patch('requests.request', return_value=reply(status=401)) as send:
                    with self.assertRaisesRegex(AuthenticationError, '未重新提交'):
                        call()
                send.assert_called_once()
        self.assertEqual(self.password_login.call_count, 2)

    def test_missing_session_renews_before_first_request(self):
        self.original['bcs']['limit_key'] = ''
        atomic_dump_json(self.path, self.original)
        credentials.reload()
        with patch('requests.request', return_value=reply(code=200)) as send:
            BCSClient().post(BCS_BASE_URL + '/shopKeeper/price/batch', [])
        self.password_login.assert_called_once()
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs['headers']['Authorization'], 'Bearer new-token')

    def test_permission_and_rate_errors_do_not_renew(self):
        with patch('requests.request', return_value=reply(status=403)):
            with self.assertRaises(AuthenticationError):
                BCSClient().get(SHOP_LIST, retry=1)
        with patch('requests.request', return_value=reply(status=429)):
            with self.assertRaises(RateLimitError):
                BCSClient().get(SHOP_LIST, retry=1)
        self.password_login.assert_not_called()

    def test_disabled_or_temporary_clients_do_not_renew(self):
        self.original['bcs']['login']['auto_refresh'] = False
        atomic_dump_json(self.path, self.original)
        credentials.reload()
        with patch('requests.request', return_value=reply(status=401)):
            with self.assertRaises(AuthenticationError):
                BCSClient().get(SHOP_LIST)
            with patch.object(credentials, 'get', side_effect=AssertionError('temporary session is isolated')):
                with self.assertRaises(AuthenticationError):
                    BCSClient(NEW_SESSION).get(SHOP_LIST)
        self.password_login.assert_not_called()

    def test_parallel_expired_requests_reuse_one_update(self):
        expired = credentials.get()
        with ThreadPoolExecutor(max_workers=4) as pool:
            renewed = list(pool.map(lambda _: auth.refresh_login(expected=expired), range(4)))
        self.assertEqual([cred.token for cred in renewed], ['new-token'] * 4)
        self.password_login.assert_called_once()
        self.verify.assert_called_once()

    def test_account_mismatch_preserves_credentials_and_blocks_further_automatic_attempts(self):
        self.verify.return_value = reply(code=200, data={'userName': 'different-account'})
        original_bytes = self.path.read_bytes()
        with patch('requests.request', return_value=reply(status=401)) as send:
            for _ in range(2):
                with self.assertRaisesRegex(AuthenticationError, '归属'):
                    BCSClient().get(SHOP_LIST)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.password_login.assert_called_once()

    def test_persisted_merge_preserves_updates_to_other_platforms(self):
        def verify(*args, **kwargs):
            atomic_dump_json(self.path, {**self.original, 'mabang': {'concurrent': 'field'}})
            return reply(code=200, data={'userName': USERNAME})

        self.verify.side_effect = verify
        auth.refresh_login()
        self.assertEqual(safe_load_json(self.path)['mabang'], {'concurrent': 'field'})

    def test_valid_har_command_only_replaces_bcs_session(self):
        har_path = Path(paths.HAR_DIR) / 'fixture.har'
        atomic_dump_json(har_path, login_har())
        result = dispatch(build_parser().parse_args(['bcs-login', '--from-har', har_path.name]))
        self.assertEqual(result, 0)
        self.assertEqual(safe_load_json(self.path)['bcs']['cookie_extra'], 'acw_tc=new-cookie')
        self.assertNotIn('har-only-password', self.path.read_text(encoding='utf-8'))
        self.password_login.assert_not_called()

    def test_har_mismatch_preserves_existing_session(self):
        original_bytes = self.path.read_bytes()
        har_path = self.path.with_name('fixture.har')
        for capture in (login_har(token='different'), login_har(limit='different'), login_har(account='different')):
            atomic_dump_json(har_path, capture)
            with self.assertRaisesRegex(AuthenticationError, '匹配'):
                auth.refresh_login(har=har_path)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.password_login.assert_not_called()
        self.verify.assert_not_called()

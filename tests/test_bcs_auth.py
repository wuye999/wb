"""Offline BCS login fixtures and automatic renewal behavior."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest import TestCase
from unittest.mock import patch

import requests

from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError, StorageLockError
from sellerops.core.files import atomic_dump_json, safe_load_json
from sellerops.integrations.bcs import auth
from sellerops.integrations.bcs import budget as bcs_budget
from sellerops.integrations.bcs.budget import RequestBudget, SyncInterrupted
from sellerops.integrations.bcs.session import BCS_BASE_URL, LOGIN_URL, BCSSession
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
        with self.assertRaisesRegex(AuthenticationError, 'login bcs'):
            auth.password_login(USERNAME, PASSWORD)
        self.login_post.assert_not_called()

    def test_gateway_challenge_stops_before_password_submission(self):
        self.login_get.side_effect = [reply(), reply(status=405, msg=PASSWORD)]
        with self.assertRaisesRegex(AuthenticationError, '网关校验') as error:
            auth.password_login(USERNAME, PASSWORD)
        self.assertNotIn(PASSWORD, str(error.exception))
        self.login_post.assert_not_called()

    def test_auth_http_requests_share_remaining_deadline(self):
        with patch.object(bcs_budget.time, 'monotonic', side_effect=[0, 2, 4, 6]):
            self.login_get.side_effect = [reply(), reply(code=200, needSlider=False)]
            with self.assertRaises(SyncInterrupted):
                auth.password_login(USERNAME, PASSWORD, budget=RequestBudget(5))
        self.assertEqual(self.business_get.call_args.kwargs['timeout'], (2.5, 2.5))
        self.assertEqual([c.kwargs['timeout'] for c in self.login_get.call_args_list], [(1.5, 1.5), (0.5, 0.5)])
        self.login_post.assert_not_called()


class TestBCSRefresh(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(directory)
        self.path = Path(paths.current.credentials_json)
        self.original = {'bcs': {'token': 'old-token', 'limit_key': 'old-limit', 'cookie_extra': 'acw_tc=old',
                                 'login': {'username': USERNAME, 'password': PASSWORD}},
                         'wb': {'shops': []}, 'mabang': {'unrelated': 'preserve'}}
        atomic_dump_json(self.path, self.original)
        credentials.reload()
        self.password_login = self.enterContext(patch.object(auth, 'password_login', return_value=NEW_SESSION))
        self.verify = self.enterContext(patch.object(auth.requests, 'get', return_value=reply(
            code=200, data={'userName': USERNAME})))

    def test_named_writes_use_the_existing_wire_format_once(self):
        stock = [{'chrtId': 303, 'amount': 0}]
        price = [{'nmID': 101, 'price': 12, 'discount': 40, 'clubDiscount': None}]
        client = BCSClient(NEW_SESSION)
        with patch.object(requests, 'request', return_value=reply(code=200, msg='fixture')) as send:
            for func, args, endpoint, payload in [
                (client.batch_set_prices, (42, price), 'price/batch', {'dataList': [{**price[0], 'shopId': 42}]}),
                (client.batch_set_stock, (42, 55, stock), 'stock/batchSetByChrtIdsBatch',
                 {'shopId': 42, 'warehouses': [{'warehouseId': 55, 'stockItems': stock}]}),
                (client.remove_to_trash, (42, [101]), 'clean/removeToTrash', [{'shopId': 42, 'nmId': 101}]),
            ]:
                with self.subTest(endpoint=endpoint):
                    send.reset_mock()
                    self.assertEqual(func(*args)['code'], 200)
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(send.call_args.args, ('POST', BCS_BASE_URL + '/shopKeeper/' + endpoint))
                    self.assertEqual(send.call_args.kwargs['json'], payload)
                    send.reset_mock()
                    send.side_effect = requests.Timeout()
                    with self.assertRaises(NetworkError):
                        func(*args)
                    self.assertEqual(send.call_count, 1)
                    send.side_effect = None
        self.assertNotIn('shopId', price[0])

    def test_discount_batches_keep_the_original_price_and_individual_receipts(self):
        from types import SimpleNamespace
        from sellerops.features.products import execution
        from sellerops.features.products.results import ProductResult
        items = [{'nmID': number, 'price': number + 10, 'discount': 55} for number in (1, 2, 3)]
        result = ProductResult('discount', 'bcs', SimpleNamespace(target=55))
        with patch.object(execution.time, 'sleep'), \
                patch.object(execution.bcs, 'post', side_effect=[{'code': 200, 'taskId': 'one'}, {'code': 400}]) as send:
            self.assertEqual(execution.submit_bcs_prices(42, items, result, chunk=2), items[:2])
        batches = [call.args[1]['dataList'] for call in send.call_args_list]
        expected = [{**item, 'clubDiscount': None, 'shopId': 42} for item in items]
        self.assertEqual(batches, [expected[:2], expected[2:]])
        self.assertEqual([(r.status, r.task_id) for r in result.receipts[42]],
                         [('accepted', 'one'), ('accepted', 'one'), ('failed', None)])
        self.assertIn('400', result.receipts[42][-1].message)

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
        writes = [lambda: client.batch_set_prices(42, [{'nmID': 123, 'price': 40}]),
                  lambda: client.batch_set_stock(42, 55, [{'chrtId': 303, 'amount': 0}]),
                  lambda: client.remove_to_trash(42, [123]), lambda: client.sync_shop(1)]
        for call in writes:
            with self.subTest(call=call):
                atomic_dump_json(self.path, self.original)
                credentials.reload()
                with patch('requests.request', return_value=reply(status=401)) as send:
                    with self.assertRaisesRegex(AuthenticationError, '未重新提交'):
                        call()
                send.assert_called_once()
        self.assertEqual(self.password_login.call_count, len(writes))

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

    def test_expired_read_threads_budget_through_refresh_and_profile(self):
        window = RequestBudget(monotonic() + 5)
        with patch('requests.request', side_effect=[reply(status=401), reply(code=200, rows=[])]) as send:
            self.assertEqual(BCSClient().fetch_shop_list(budget=window), [])
        self.password_login.assert_called_once_with(USERNAME, PASSWORD, budget=window)
        self.assertLessEqual(sum(self.verify.call_args.kwargs['timeout']), 5)
        self.assertLessEqual(sum(send.call_args.kwargs['timeout']), 5)

    def test_refresh_lock_wait_stops_at_budget_after_authentication_rejection(self):
        clock = [0.0]

        def blocked_acquire():
            clock[0] += lock.call_args.kwargs['timeout']
            raise StorageLockError('fixture', 0.25)

        with (patch.object(bcs_budget.time, 'monotonic', side_effect=lambda: clock[0]),
              patch.object(bcs_budget, 'FileLock') as lock,
              patch('requests.request', return_value=reply(status=401)) as send):
            lock.return_value.acquire.side_effect = blocked_acquire
            with self.assertRaises(AuthenticationError):
                BCSClient().fetch_shop_list(budget=RequestBudget(0.4))
        self.assertEqual([c.kwargs['timeout'] for c in lock.call_args_list], [0.25, 0.4 - 0.25])
        self.assertEqual(clock[0], 0.4)
        self.password_login.assert_not_called()
        send.assert_called_once()

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

    def test_browser_connection_only_replaces_bcs_session(self):
        session = BCSSession(NEW_SESSION.token, NEW_SESSION.limit_key,
                             NEW_SESSION.cookie + '; password=remembered; username=fixture; rememberMe=true')
        auth.connect_browser_session(credentials.get(), session)
        self.assertEqual(safe_load_json(self.path), {
            **self.original, 'bcs': {**self.original['bcs'], 'token': 'new-token',
                                     'limit_key': 'new-limit', 'cookie_extra': 'acw_tc=new-cookie'},
        })
        self.password_login.assert_not_called()

    def test_browser_account_mismatch_preserves_existing_session(self):
        original_bytes = self.path.read_bytes()
        self.verify.return_value = reply(code=200, data={'userName': 'different'})
        with self.assertRaisesRegex(AuthenticationError, '归属'):
            auth.connect_browser_session(credentials.get(), NEW_SESSION)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.password_login.assert_not_called()

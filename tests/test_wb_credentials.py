"""Exercise WB browser session renewal with synthetic credentials and no platform access."""
import base64
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from catalog_fixture import mock_non_git_checkout
from sellerops.cli.main import main
from sellerops.core.errors import AuthenticationError, DataConsistencyError, ValidationError
from sellerops.core.files import atomic_dump_json, safe_load_json
from sellerops.core.utils import CookieExpiredError
from sellerops.integrations.wildberries.browser_session import WBSession, WBShop
from sellerops.integrations.wildberries import auth, client, session_update
from sellerops.settings import paths

FIRST = 'aabbccdd-0000-0000-0000-000000000001'
SECOND = 'aabbccdd-0000-0000-0000-000000000002'


def jwt(data):
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return f'e30.{payload}.fixture'


def lk(supplier):
    return jwt({'data': {'Z-Sid': supplier}})


def response(status, data=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode()
    return result


class TestWBCredentials(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(directory.name)
        self.path = Path(paths.current.credentials_json)
        self.token = jwt({'user': 'shared-account', 'session_id': 'new'})
        self.template = f'__zzatw-wb=new==; x-supplier-id-external= {FIRST} ; external-locale=ru'
        self.original = {
            'wb': {'root_version': 'fixture-version', 'shops': [
                {'shopId': 11, 'shopName': '第一店', 'wb_seller_lk': lk(FIRST),
                 'authorizev3': jwt({'user': 'shared-account'}), 'custom': 'preserve'},
                {'shopId': 12, 'shopName': '第二店', 'wb_seller_lk': 'damaged',
                 'cookie': f'x-supplier-id-external={SECOND}'},
            ]},
            'bcs': {'token': 'fixture-bcs'}, 'mabang': {'auth_updated_at': 'before'},
        }
        atomic_dump_json(self.path, self.original)

    def update_shops(self, *, token=None, cookie=None):
        config = safe_load_json(self.path)['wb']
        session_update.update_shops(self.path, config, config['shops'],
                                    self.token if token is None else token,
                                    self.template if cookie is None else cookie)

    def test_cli_signs_and_probes_each_shop_before_one_atomic_update(self):
        session = WBSession(self.token, self.template, (WBShop(FIRST, 111, '第一店'), WBShop(SECOND, 112, '第二店')))
        calls = []

        def send(request, **kwargs):
            calls.append(request)
            self.assertEqual(request.headers['authorizev3'], self.token)
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(kwargs['timeout'], 20)
            supplier = FIRST if len(calls) <= 2 else SECOND
            if request.url == auth.AUTH_URL:
                self.assertEqual(request.method, 'POST')
                self.assertEqual(request.headers['Cookie'], self.template.replace(FIRST, supplier))
                self.assertNotIn('seller-lk', request.headers)
                self.assertEqual(json.loads(request.body), {'params': {}, 'jsonrpc': '2.0', 'id': 'bridge'})
                return response(200, {'result': {'data': {'token': lk(supplier)}}})
            self.assertEqual(request.method, 'GET')
            self.assertTrue(request.url.startswith(client.QUESTIONS_URL + '?limit=5&'))
            self.assertEqual(request.headers['seller-lk'], lk(supplier))
            self.assertEqual(request.headers['root-version'], 'fixture-version')
            self.assertIn('x-supplier-id-external=' + supplier, request.headers['Cookie'])
            current = safe_load_json(self.path)
            self.assertEqual(current['wb'], self.original['wb'])
            current['mabang']['auth_updated_at'] = 'during'
            atomic_dump_json(self.path, current)
            return (response(204) if supplier == FIRST
                    else response(200, {'error': False, 'data': {'questions': []}}))

        output = StringIO()
        with (patch('sellerops.features.system.login._browser_session', return_value=session),
              patch.object(requests.Session, 'send', side_effect=send), redirect_stdout(output)):
            self.assertEqual(main(['--data-dir', paths.current.data_dir, 'login', 'wb', '--resume']), 0)
        self.assertEqual([request.method for request in calls], ['POST', 'GET', 'POST', 'GET'])
        saved = safe_load_json(self.path)
        for shop, supplier in zip(saved['wb']['shops'], [FIRST, SECOND]):
            self.assertEqual(shop['authorizev3'], self.token)
            self.assertEqual(shop['wb_seller_lk'], lk(supplier))
            self.assertEqual(shop['cookie'], self.template.replace(FIRST, supplier))
        self.assertEqual(saved['wb']['shops'][0]['custom'], 'preserve')
        self.assertEqual(saved['bcs'], self.original['bcs'])
        self.assertEqual(saved['mabang']['auth_updated_at'], 'during')
        self.assertIn('11, 12', output.getvalue())
        self.assertNotIn(self.token, output.getvalue())

    def test_invalid_session_and_local_conflicts_fail_before_http(self):
        cases = [
            ({'token': 'a.b.c'}, {}, 'WB 登录会话缺少有效凭证'),
            ({'cookie': self.template + f'; x-supplier-id-external={SECOND}'}, {}, '重复'),
            ({}, {'cookie': f'x-supplier-id-external={FIRST}'}, 'UUID 与其他店铺重复'),
            ({}, {'wb_seller_lk': lk(FIRST)}, 'Z-Sid 冲突'),
            ({}, {'authorizev3': jwt({'user': 'other-account'})}, '登录账号不一致'),
        ]
        for session, second, error in cases:
            with self.subTest(error=error):
                config = safe_load_json(self.path)
                config['wb'] = {**self.original['wb'], 'shops': [
                    self.original['wb']['shops'][0], {**self.original['wb']['shops'][1], **second},
                ]}
                atomic_dump_json(self.path, config)
                before = self.path.read_bytes()
                with patch.object(requests.Session, 'send') as send, self.assertRaisesRegex(ValidationError, error):
                    self.update_shops(**session)
                send.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_second_shop_failure_preserves_whole_file_and_hides_response(self):
        for new_token, probe_status, expected in [(lk(FIRST), 204, 'Z-Sid'), (lk(SECOND), 403, 'HTTP 403')]:
            with self.subTest(expected=expected):
                before = self.path.read_bytes()
                responses = [response(200, {'result': {'data': {'token': lk(FIRST)}}}), response(204),
                             response(200, {'result': {'data': {'token': new_token}}}),
                             response(probe_status, {'secret': self.token})]
                with patch.object(requests.Session, 'send', side_effect=responses) as send, \
                        self.assertRaisesRegex(ValidationError, f'店铺 12.*{expected}') as error:
                    self.update_shops()
                self.assertNotIn(self.token, str(error.exception))
                self.assertEqual(send.call_count, 3 if expected == 'Z-Sid' else 4)
                self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_shop_edit_is_preserved(self):
        def probe(*_):
            current = safe_load_json(self.path)
            current['wb']['shops'][0]['custom'] = 'concurrent-edit'
            atomic_dump_json(self.path, current)

        with patch.object(auth, 'sign_seller_token', side_effect=[lk(FIRST), lk(SECOND)]), \
                patch.object(auth, 'probe_session', side_effect=probe), \
                self.assertRaisesRegex(DataConsistencyError, 'wb.shops 在签发期间已变更'):
            self.update_shops()
        self.assertEqual(safe_load_json(self.path)['wb']['shops'][0],
                         {**self.original['wb']['shops'][0], 'custom': 'concurrent-edit'})

    def test_wb_auth_errors_stop_before_retry_or_discount_confirmation(self):
        for status, error in [(401, AuthenticationError), (403, CookieExpiredError)]:
            with self.subTest(status=status):
                session = Mock()
                session.request.return_value = session.post.return_value = response(status)
                with self.assertRaisesRegex(error, 'login wb'):
                    client.request_post(session, 'https://fixture.invalid', {})
                with patch.object(client, 'make_session', return_value=session), self.assertRaises(error):
                    client.WBClient({'shopId': 11}).upload_batch_discount([{}])
                self.assertEqual(session.request.call_count, 1)

    def test_doctor_skips_template_and_cross_shop_conflicts_and_reports_verified_shops(self):
        mock_non_git_checkout(self)
        third = 'aabbccdd-0000-0000-0000-000000000003'
        shared_token = 'private-shared-seller-lk'
        authorize = 'private-shared-authorize'

        def shop(shop_id, seller_lk, cookie):
            return {'shopId': shop_id, 'wb_seller_lk': seller_lk, 'cookie': cookie, 'authorizev3': authorize}

        rows = [
            {'shopId': 1, 'shopName': '示例店铺'},
            shop(11, 'private-lk-11', f'x-supplier-id-external={FIRST.upper()}'),
            shop(12, lk(FIRST), 'session=private-cookie-12'),
            shop(21, shared_token, f'x-supplier-id-external={SECOND}'),
            shop(22, shared_token, 'session=private-cookie-22'),
            shop(31, lk(third), f'x-supplier-id-external={third}'),
            shop(32, 'private-lk-32', 'session=private-cookie-32'),
            shop(33, 'private-lk-33', 'session=private-cookie-33'),
            shop(34, 'private-lk-34', 'session=private-cookie-34'),
            {'shopId': 35, 'wb_seller_lk': '   '},
            shop('private-malformed-id', 'private-bad-token', 'session=private-bad'),
        ]
        atomic_dump_json(self.path, {'wb': {'shops': rows}})
        before = self.path.read_bytes()

        def send(session, prepared, **kwargs):
            self.assertEqual(prepared.method, 'GET')
            self.assertFalse(kwargs['allow_redirects'])
            return response({31: 204, 32: 401, 33: 403, 34: 503}[session._sellerops_shop])

        output, terminal = StringIO(), StringIO()
        with (patch.object(requests.Session, 'send', autospec=True, side_effect=send) as http,
              redirect_stdout(output), redirect_stderr(terminal)):
            code = main(['--data-dir', paths.current.data_dir, 'doctor', '--check-online', '--json'])
        self.assertEqual(code, 0)
        body = json.loads(output.getvalue())['data']
        self.assertEqual(body['credentials']['warnings'], [
            {'path': 'wb.shops', 'code': 'legacy_wb_template_shop', 'shop_ids': [1]},
            {'path': 'wb.shops', 'code': 'duplicate_wb_supplier_uuid', 'shop_ids': [11, 12]},
            {'path': 'wb.shops', 'code': 'duplicate_wb_seller_lk', 'shop_ids': [21, 22]},
        ])
        self.assertEqual(body['online']['wb'], {
            '11': 'unavailable', '12': 'unavailable', '21': 'unavailable', '22': 'unavailable',
            '31': 'verified', '32': 'authentication_failed', '33': 'authentication_failed',
            '34': 'unavailable', '35': 'missing',
        })
        self.assertEqual([call.args[0]._sellerops_shop for call in http.call_args_list], [31, 32, 33, 34])
        self.assertIn('店铺 ID 11、12', terminal.getvalue())
        public = output.getvalue() + terminal.getvalue()
        for secret in (FIRST, FIRST.upper(), SECOND, third, lk(FIRST), shared_token, authorize, 'private-'):
            self.assertNotIn(secret, public)
        self.assertEqual(self.path.read_bytes(), before)

    def test_doctor_keeps_real_shop_one_even_with_sample_name(self):
        mock_non_git_checkout(self)
        atomic_dump_json(self.path, {'wb': {'shops': [
            {'shopId': 1, 'shopName': '示例店铺', 'cookie': f'x-supplier-id-external={FIRST}'},
        ]}})
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            self.assertEqual(main(['--data-dir', paths.current.data_dir, 'doctor', '--json']), 0)
        body = json.loads(output.getvalue())['data']
        self.assertEqual(body['credentials']['warnings'], [])
        self.assertEqual(body['shop_counts']['credential_configured'], 1)

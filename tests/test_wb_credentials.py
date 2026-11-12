"""Exercise one-capture WB renewal with synthetic credentials and no platform access."""
import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from sellerops.cli.main import main
from sellerops.core.errors import AuthenticationError, DataConsistencyError, ValidationError
from sellerops.core.files import atomic_dump_json, safe_load_json
from sellerops.core.utils import CookieExpiredError
from sellerops.integrations.sessions import capture
from sellerops.integrations.wildberries import auth, client
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
        self.addCleanup(paths.configure, paths.DATA_DIR)
        paths.configure(directory.name)
        self.path = Path(paths.CREDENTIALS_JSON)
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

    def fetch(self, *, token=None, cookie=None):
        headers = {'AuthorizeV3': token or self.token, 'Cookie': cookie or self.template}
        return f'```js\nfetch("https://seller.wildberries.ru/test", {json.dumps({"headers": headers})});\n```'

    def test_cli_signs_and_probes_each_shop_before_one_atomic_update(self):
        har = Path(paths.HAR_DIR)
        har.mkdir()
        (har / 'session.md').write_text(self.fetch(), encoding='utf-8-sig')
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
        with patch.object(requests.Session, 'send', side_effect=send), redirect_stdout(output):
            self.assertEqual(main(['--data-dir', paths.DATA_DIR, 'cookies-update', 'session.md']), 0)
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

    def test_invalid_capture_and_local_conflicts_fail_before_http(self):
        cases = [
            (self.fetch(token='a.b.c'), {}, 'authorizev3'),
            (self.fetch() + '\n' + self.fetch(), {}, '一个 Copy as fetch'),
            (self.fetch(cookie=self.template + f'; x-supplier-id-external={SECOND}'), {}, '重复'),
            (self.fetch(cookie=self.template.replace(FIRST, '12345678-0000-0000-0000-000000000001')),
             {}, '不在本地'),
            (self.fetch(), {'cookie': f'x-supplier-id-external={FIRST}'}, 'UUID 与其他店铺重复'),
            (self.fetch(), {'wb_seller_lk': lk(FIRST)}, 'Z-Sid 冲突'),
            (self.fetch(), {'authorizev3': jwt({'user': 'other-account'})}, '登录账号不一致'),
        ]
        for text, second, error in cases:
            with self.subTest(error=error):
                config = safe_load_json(self.path)
                config['wb'] = {**self.original['wb'], 'shops': [
                    self.original['wb']['shops'][0], {**self.original['wb']['shops'][1], **second},
                ]}
                atomic_dump_json(self.path, config)
                before = self.path.read_bytes()
                with patch.object(requests.Session, 'send') as send, self.assertRaisesRegex(ValidationError, error):
                    capture.refresh(text, self.path)
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
                    capture.refresh(self.fetch(), self.path)
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
            capture.refresh(self.fetch(), self.path)
        self.assertEqual(safe_load_json(self.path)['wb']['shops'][0],
                         {**self.original['wb']['shops'][0], 'custom': 'concurrent-edit'})

    def test_wb_auth_errors_stop_before_retry_or_discount_confirmation(self):
        for status, error in [(401, AuthenticationError), (403, CookieExpiredError)]:
            with self.subTest(status=status):
                session = Mock()
                session.request.return_value = session.post.return_value = response(status)
                with self.assertRaisesRegex(error, 'cookies-update'):
                    client.request_post(session, 'https://fixture.invalid', {})
                with patch.object(client, 'make_session', return_value=session):
                    result = client.WBClient({'shopId': 11}).upload_batch_discount([{}])
                self.assertFalse(result.success)
                self.assertIn('cookies-update', result.error_message)
                session.post.assert_called_once()
                session.request.assert_called_once()

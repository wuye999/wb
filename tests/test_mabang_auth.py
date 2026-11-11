"""Mabang login, auth recovery and session storage without real credentials/network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, local
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from sellerops.integrations.mabang import auth, client, forecast
from sellerops.integrations.mabang.capture import read_har
from sellerops.integrations.mabang.session import (
    AAMZ_BASE, LOGIN_KEY, WWW_BASE, MabangAuthError, MabangVerificationRequired,
    auth_signature, new_session, request,
)
from sellerops.settings import credentials, paths


USER = 'fixture-account'
PASSWORD = 'p@ss+%2B字'
FORECAST_URL = AAMZ_BASE + '?mod=uploadforecastorderv2.list&key=new-server-key&k=dynamic-signature'


def _cookie(name, value, host, *, expires=None):
    return dict(name=name, value=value, domain=host, path='/', hostOnly=not host.startswith('.'),
                secure=True, httpOnly=True, expires=expires)


def _set_cookie(session, name, value, host):
    c = requests.cookies.create_cookie(name, value, domain=host, path='/', secure=True,
                                       expires=int(time.time()) + 3600)
    c.domain_specified = host.startswith('.')
    session.cookies.set_cookie(c)


def _response(prepared, value, status=200, headers=None):
    response = requests.Response()
    response.request = prepared
    response.url = prepared.url
    response.status_code = status
    response.encoding = 'utf-8'
    response.headers.update(headers or {})
    response._content = (value if isinstance(value, str) else json.dumps(value)).encode('utf-8')
    return response


def _har() -> dict:
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=12)).isoformat()
    www = 'www.mabangerp.com'
    aamz = 'aamz.mabangerp.com'

    def entry(method, url, body, *, cookies=(), sets=(), post=None):
        req = dict(method=method, url=url, cookies=list(cookies), headers=[
            {'name': 'User-Agent', 'value': 'fixture-agent'}, {'name': 'Content-Length', 'value': '10'}])
        if post:
            req['postData'] = dict(mimeType='application/x-www-form-urlencoded; charset=UTF-8', text=urlencode(post),
                                   params=[{'name': 'password', 'value': 'incorrect HAR params projection'}])
        return dict(startedDateTime=now.isoformat(), request=req,
                    response=dict(status=200, headers=[{'name': 'Set-Cookie', 'value': s} for s in sets],
                                  content={'text': body if isinstance(body, str) else json.dumps(body)}))

    return {'log': {'entries': [
        entry('POST', WWW_BASE + '?mod=main.doLogin', {'success': True},
              cookies=[_cookie('PHPSESSID', 'www-php', www, expires=expires)],
              sets=[f'{LOGIN_KEY}=new-key; Max-Age=43200; Secure; HttpOnly'],
              post={'username': USER, 'password': PASSWORD, 'timezone': 'UTC+8'}),
        entry('GET', WWW_BASE + '?mod=main.gotoApp&v=v3&menuKey=M0010212',
              f'<iframe src="{escape(FORECAST_URL)}"></iframe>'),
        entry('GET', FORECAST_URL, '<html>forecast</html>',
              sets=['PHPSESSID=aamz-php; Max-Age=43200; Path=/; HttpOnly; Secure',
                    f'{LOGIN_KEY}=new-key; Max-Age=43200; Secure', 'discarded=gone; Max-Age=0']),
        entry('POST', AAMZ_BASE + '?mod=uploadforecastorderv2.getForecastOrderList',
              {'success': True, 'orderList': []}, cookies=[_cookie('PHPSESSID', 'aamz-php', aamz, expires=expires)]),
    ]}}


class TestMabangAuth(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = paths.DATA_DIR
        paths.configure(temporary.name)
        self.addCleanup(paths.configure, previous)
        for obj, name, value in [(credentials, '_CRED', None), (client, '_local', local())]:
            patcher = patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = Path(paths.CREDENTIALS_JSON)
        self.capture = self.path.with_name('fixture.har')
        self.capture.write_text(json.dumps(_har()), encoding='utf-8')
        self.calls = []

    def seed(self, *, managed=True, passport=False, expires_in=3600) -> dict:
        cred = {'www_cookie': f'PHPSESSID=old-www; {LOGIN_KEY}=old-key',
                'aamz_cookie': f'PHPSESSID=old-aamz; {LOGIN_KEY}=old-key',
                'api_key': 'old-key', 'api_bearer': 'old-token',
                'shop_map': {'fixture': '示例(1)'}, 'warehouse_id': 42}
        if managed:
            expiry = int(time.time()) + expires_in
            cred['login'] = dict(username=USER, password=PASSWORD, timezone='UTC+8', auto_refresh=True,
                                 passport_sso=passport, headers={'user-agent': 'fixture-agent'})
            cred['cookie_jar'] = [_cookie(name, val, host, expires=expiry)
                                  for host, php in [('www.mabangerp.com', 'old-www'), ('aamz.mabangerp.com', 'old-aamz')]
                                  for name, val in [('PHPSESSID', php), (LOGIN_KEY, 'old-key')]]
        self.path.write_text(json.dumps({'mabang': cred, 'wb': {'keep': 'fixture'}}), encoding='utf-8')
        return cred

    def fake(self, session, prepared, **_kwargs):
        url = urlsplit(prepared.url)
        host = url.hostname
        mod = parse_qs(url.query).get('mod', [''])[0]
        self.calls.append((prepared.method, host, mod or url.path))
        sent_cookie = prepared.headers.get('Cookie', '')
        if url.path == '/index.htm':
            self.assertEqual(prepared.headers['User-Agent'], 'fixture-agent')
            if 'PHPSESSID=' not in sent_cookie:
                _set_cookie(session, 'PHPSESSID', 'fresh-www', host)
            return _response(prepared, '<html>login page</html>')
        if mod == 'main.doLogin':
            form = parse_qs(prepared.body, keep_blank_values=True)
            self.assertEqual(form['password'], [PASSWORD])
            self.assertEqual(form['timezone'], ['UTC+8'])
            self.assertEqual(form['verifyCode'], [''])
            self.assertRegex(sent_cookie, r'PHPSESSID=(old|fresh)-www')
            _set_cookie(session, LOGIN_KEY, 'new-key', host)
            return _response(prepared, {'success': True})
        if mod == 'main.plogin':
            return _response(prepared, '', 302, {'Location': 'https://passport.mabangerp.com/interface/index.php?sign=temporary'})
        if host == 'passport.mabangerp.com':
            self.assertNotIn('PHPSESSID=old-www', sent_cookie)
            self.assertNotIn('PHPSESSID=old-aamz', sent_cookie)
            _set_cookie(session, 'PHPSESSID', 'passport-php', host)
            return _response(prepared, '', 302, {'Location': WWW_BASE + '?mod=main.doMabangLogin&cKey=temporary'})
        if mod == 'main.doMabangLogin':
            self.assertNotIn('passport-php', sent_cookie)
            return _response(prepared, '<html>home</html>')
        if mod == 'main.gotoApp':
            self.assertIn(f'{LOGIN_KEY}=new-key', sent_cookie)
            return _response(prepared, f'<iframe src="{escape(FORECAST_URL)}"></iframe>')
        if mod == 'uploadforecastorderv2.list':
            self.assertEqual(prepared.url, FORECAST_URL)
            self.assertNotIn('PHPSESSID=old-www', sent_cookie)
            self.assertNotIn('passport-php', sent_cookie)
            _set_cookie(session, 'PHPSESSID', 'new-aamz', host)
            _set_cookie(session, LOGIN_KEY, 'new-key', host)
            return _response(prepared, '<html>forecast</html>')
        if url.path.endswith('/getTokenByKey'):
            key = json.loads(prepared.body)['key']
            self.assertIn(key, {'new-key', 'old-key'})
            return _response(prepared, {'code': 200, 'data': {'token': key + '-token'}})
        self.fail('Unexpected synthetic HTTP endpoint')

    def test_har_preserves_encoding_cookie_scope_expiry_and_required_flow(self):
        result = read_har(self.capture)
        self.assertEqual(result['login']['password'], PASSWORD)
        self.assertEqual(result['login']['timezone'], 'UTC+8')
        self.assertEqual(result['login']['headers'], {'user-agent': 'fixture-agent'})
        self.assertIn('PHPSESSID=www-php', result['www_cookie'])
        self.assertIn('PHPSESSID=aamz-php', result['aamz_cookie'])
        self.assertNotIn('www-php', result['aamz_cookie'])
        self.assertNotIn('discarded', result['aamz_cookie'])
        self.assertTrue(all(c['expires'] > time.time() for c in result['cookie_jar']))
        incomplete = _har()
        incomplete['log']['entries'].pop()
        self.capture.write_text(json.dumps(incomplete), encoding='utf-8')
        with self.assertRaises(MabangAuthError):
            read_har(self.capture)

    def test_har_import_exchanges_token_without_password_login(self):
        cred = self.seed(managed=False)
        with patch.object(requests.Session, 'send', autospec=True, side_effect=self.fake):
            auth.refresh_login(cred, har=self.capture)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(cred['api_key'], 'new-key')
        self.assertEqual(cred['api_bearer'], 'new-key-token')
        stored = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(stored['wb'], {'keep': 'fixture'})
        self.assertEqual(stored['mabang']['warehouse_id'], 42)

    def test_password_login_uses_new_frame_and_supports_passport(self):
        for passport in (False, True):
            with self.subTest(passport=passport):
                cred = self.seed(passport=passport)
                self.calls.clear()
                with patch.object(requests.Session, 'send', autospec=True, side_effect=self.fake):
                    auth.refresh_login(cred)
                self.assertEqual(cred['api_bearer'], 'new-key-token')
                self.assertIn('PHPSESSID=new-aamz', cred['aamz_cookie'])
                self.assertEqual(any(c[1] == 'passport.mabangerp.com' for c in self.calls), passport)
                self.assertEqual(sum(c[2] == 'main.doLogin' for c in self.calls), 1)

    def test_verification_or_token_failure_preserves_previous_complete_credentials(self):
        for code in ('CHECK_VERIFY_CODE', 2001, 'token-failure'):
            with self.subTest(code=code):
                cred = self.seed()
                original = self.path.read_bytes()

                def send(session, prepared, **kwargs):
                    if code != 'token-failure' and 'mod=main.doLogin' in prepared.url:
                        return _response(prepared, {'success': False, 'errCode': code, 'message': PASSWORD})
                    if code == 'token-failure' and '/getTokenByKey' in prepared.url:
                        return _response(prepared, {'code': 500, 'data': {'token': PASSWORD}})
                    return self.fake(session, prepared, **kwargs)

                expected = MabangAuthError if code == 'token-failure' else MabangVerificationRequired
                with patch.object(requests.Session, 'send', autospec=True, side_effect=send), \
                        self.assertRaises(expected) as error:
                    auth.refresh_login(cred)
                self.assertNotIn(PASSWORD, str(error.exception))
                self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_refresh_is_deduplicated(self):
        original = self.seed()
        signature = auth_signature(original)
        gate = Barrier(2)

        def work():
            cred = dict(original)
            gate.wait(timeout=5)
            auth.refresh_login(cred, expected=signature)
            return cred['api_bearer']

        with patch.object(requests.Session, 'send', autospec=True, side_effect=self.fake), \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(work) for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], ['new-key-token'] * 2)
        self.assertEqual(sum(c[2] == 'main.doLogin' for c in self.calls), 1)

    def test_read_retries_once_after_explicit_expiry(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                cred = self.seed()
                count = 0
                self.calls.clear()

                def send(session, prepared, **kwargs):
                    nonlocal count
                    if 'mod=order.oTc' in prepared.url:
                        count += 1
                        if count == 1 or not succeeds:
                            return _response(prepared, {'success': False, 'message': '您的登录信息已过期'})
                        self.assertIn(f'{LOGIN_KEY}=new-key', prepared.headers['Cookie'])
                        return _response(prepared, {'success': True, 'orderDataList': []})
                    return self.fake(session, prepared, **kwargs)

                with patch.object(requests.Session, 'send', autospec=True, side_effect=send):
                    if succeeds:
                        self.assertTrue(client.form_request(cred, WWW_BASE, 'order.oTc', read_only=True).ok)
                    else:
                        with self.assertRaises(MabangAuthError):
                            client.form_request(cred, WWW_BASE, 'order.oTc', read_only=True)
                self.assertEqual(count, 2)
                self.assertEqual(sum(c[2] == 'main.doLogin' for c in self.calls), 1)

    def test_permission_failure_and_non_login_html_do_not_refresh(self):
        cred = self.seed()
        for payload, status in [({'success': False, 'message': '无权限'}, 403), ('<html>维护中</html>', 200)]:
            with self.subTest(status=status):
                def send(_session, prepared, **_kwargs):
                    return _response(prepared, payload, status)

                with patch.object(requests.Session, 'send', autospec=True, side_effect=send) as http:
                    result = client.form_request(cred, WWW_BASE, 'order.oTc', read_only=True)
                self.assertEqual(result.status_code, status)
                self.assertEqual(http.call_count, 1)

    def test_write_prepares_session_but_never_replays_after_401(self):
        cred = self.seed(expires_in=120)
        business = []

        def send(session, prepared, **kwargs):
            if 'mod=ordera.getForecastLogistics' in prepared.url:
                business.append('probe')
                self.assertIn(f'{LOGIN_KEY}=new-key', prepared.headers['Cookie'])
                return _response(prepared, {'success': True})
            if 'mod=ordera.doBatchCreateForecast' in prepared.url:
                business.append('write')
                return _response(prepared, {'success': False}, 401)
            return self.fake(session, prepared, **kwargs)

        with patch.object(requests.Session, 'send', autospec=True, side_effect=send), \
                self.assertRaisesRegex(MabangAuthError, '未重放'):
            forecast.batch_create_forecast(cred, ['fixture-order'], 'fixture-logistics', 'fixture-channel')
        self.assertEqual(business, ['probe', 'write'])
        self.assertEqual(sum(c[2] == 'main.doLogin' for c in self.calls), 1)

    def test_legacy_api_read_renews_bearer_without_password_login(self):
        cred = self.seed(managed=False)
        bearers = []

        def send(session, prepared, **kwargs):
            if '/v2/' in prepared.url:
                self.assertEqual(prepared.headers['key'], 'old-key')
                bearers.append(prepared.headers['Authorization'])
                status = 401 if len(bearers) == 1 else 200
                return _response(prepared, {'code': status}, status)
            return self.fake(session, prepared, **kwargs)

        with patch.object(requests.Session, 'send', autospec=True, side_effect=send):
            self.assertTrue(client.api_post(cred, '/fixture-read', {}, read_only=True).ok)
        self.assertEqual(bearers, ['Bearer old-token', 'Bearer old-key-token'])
        self.assertEqual(len(self.calls), 1)

    def test_redirect_cannot_send_credentials_to_another_origin(self):
        def send(_session, prepared, **_kwargs):
            return _response(prepared, '', 307, {'Location': 'https://untrusted.invalid/receive?secret=private'})

        with new_session({}) as session, patch.object(requests.Session, 'send', autospec=True, side_effect=send) as http:
            with self.assertRaises(MabangAuthError) as error:
                request(session, 'POST', WWW_BASE, headers={}, data={'password': PASSWORD})
        self.assertNotIn('private', str(error.exception))
        self.assertEqual(http.call_count, 1)

    def test_auth_error_does_not_fall_back_to_configured_handover_channel(self):
        with patch.object(forecast, 'discover_handover_channel', return_value=None), \
                patch.object(forecast, 'form_request', side_effect=MabangAuthError('需要验证')):
            with self.assertRaises(MabangAuthError):
                forecast.get_handover_channel_value(
                    {'handover_channel_id': 'fixture', 'handover_channel_value': 'fallback'}, 'order')

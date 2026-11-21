"""Password and browser login with coherent web/API credential rotation."""
from __future__ import annotations

import json
import time
from copy import deepcopy
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import requests

from sellerops.integrations.mabang.session import (
    AAMZ_BASE, SSO_GET_TOKEN_URL, WWW_BASE, MabangAuthError,
    auth_expired, auth_signature, cookie_header, cookie_key, headers, new_session, request,
    response_json, verification_error,
)
from sellerops.integrations.mabang.state import auth_lock, commit_auth, reload_auth, snapshot


class MabangKeyRejected(MabangAuthError):
    """SSO rejected the web session key rather than failing for an unrelated reason."""


class _ForecastFrame(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != 'iframe':
            return
        src = dict(attrs).get('src') or ''
        url = urlsplit(src)
        if (url.scheme == 'https' and url.hostname == 'aamz.mabangerp.com'
                and not url.username and not url.password and url.path == '/index.php'
                and parse_qs(url.query).get('mod') == ['uploadforecastorderv2.list']):
            self.urls.add(src)


def _forecast_frame(html: str) -> str:
    parser = _ForecastFrame()
    parser.feed(html)
    if len(parser.urls) != 1:
        raise MabangAuthError('未在马帮页面找到唯一的上传预报入口。')
    return next(iter(parser.urls))


def auto_enabled(cred: dict) -> bool:
    login = cred.get('login') or {}
    return bool(login.get('auto_refresh') and login.get('username') and login.get('password'))


def api_profile(cred: dict) -> dict:
    profile = headers(cred, 'api.mabangerp.com', navigation=True)
    return {**{k: v for k, v in profile.items() if not k.startswith('Sec-Fetch-')},
            'Accept': 'application/json, text/plain, */*', 'Content-Type': 'application/json',
            'ProjectId': 'erp', 'cluster-id': '1', 'lang': 'cn', 'TimeZone': 'UTC+8'}


def exchange_token(cred: dict, session: requests.Session, key: str) -> str:
    if not key:
        raise MabangKeyRejected('马帮 www 会话缺少 API 登录 key。')
    try:
        response = request(session, 'POST', SSO_GET_TOKEN_URL, headers=api_profile(cred),
                           data=json.dumps({'key': key, 'lang': 'zh'}))
        if auth_expired(response):
            raise MabangKeyRejected('马帮 API 拒绝了当前登录 key。')
        if not response.ok:
            raise MabangAuthError(f'马帮 API 换取 Bearer 失败（HTTP {response.status_code}）。')
        result = response_json(response)
    except MabangAuthError:
        raise
    except (requests.RequestException, RuntimeError):
        raise MabangAuthError('马帮 API 换取 Bearer 失败；原完整凭证保留。') from None
    data = result.get('data')
    token = data.get('token') if isinstance(data, dict) else None
    if result.get('code') == 200 and isinstance(token, str) and token:
        return token
    msg = str(result.get('message') or result.get('msg') or '').lower()
    if any(s in msg for s in ('key无效', 'key已失效', '无效的key', 'invalid key', 'key expired')):
        raise MabangKeyRejected('马帮 API 拒绝了当前登录 key。')
    raise MabangAuthError('马帮 API 未返回有效 Bearer；原完整凭证保留。')


def _page(response: requests.Response, stage: str) -> None:
    if not response.ok or auth_expired(response):
        raise MabangAuthError(f'马帮{stage}未建立已登录会话。')


def _password_login(candidate: dict, session: requests.Session) -> None:
    login = candidate.get('login') or {}
    if not (login.get('username') and login.get('password')):
        raise MabangAuthError('未配置马帮账号密码，请在本机配置后运行 login mabang。')
    nav = headers(candidate, 'www.mabangerp.com', navigation=True)
    stage = '打开登录页'
    try:
        # The password response does not necessarily renew the www PHP session.
        for cookie in list(session.cookies):
            if (cookie.name == 'PHPSESSID' and cookie.domain.lstrip('.') == 'www.mabangerp.com'
                    and cookie.expires is not None and cookie.expires <= time.time() + 300):
                session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        landing = request(session, 'GET', 'https://www.mabangerp.com/index.htm', headers=nav)
        if not landing.ok:
            raise MabangAuthError(f'马帮打开登录页失败（HTTP {landing.status_code}）。')
        stage = '提交密码登录'
        response = request(session, 'POST', WWW_BASE, params={'mod': 'main.doLogin'},
                           headers=headers(candidate, 'www.mabangerp.com', login=True),
                           data={'username': login['username'], 'password': login['password'],
                                 'verifyType': '1', 'mobile': '', 'acceptSmsMobile': '', 'verifyCode': '',
                                 'remember': '1', 'allowImageCode': '1', 'loginEntrance': '1',
                                 'timezone': login.get('timezone') or 'UTC+8'})
        if not response.ok:
            auth_expired(response)  # A challenge can also arrive in a non-2xx JSON response.
            raise MabangAuthError(f'马帮密码登录失败（HTTP {response.status_code}）。')
        result = response_json(response)
        code = result.get('errCode')
        if code == 'CHECK_VERIFY_CODE':
            raise verification_error('腾讯验证码')
        if str(code) == '2001':
            raise verification_error('短信验证')
        if result.get('success') is not True:
            if str(code) == '2000':
                raise MabangAuthError('马帮要求在网页更新密码，原完整凭证保留。')
            raise MabangAuthError('马帮未接受密码登录，请核对账号、密码及登录权限。')
        if login.get('passport_sso'):
            stage = '完成 passport 单点登录'
            _page(request(session, 'GET', WWW_BASE, params={'mod': 'main.plogin'}, headers=nav), stage)
        stage = '打开预报菜单'
        menu = request(session, 'GET', WWW_BASE, headers=nav,
                       params={'mod': 'main.gotoApp', 'v': 'v3', 'menuKey': 'M0010212'})
        _page(menu, stage)
        target = _forecast_frame(menu.text)
        stage = '建立预报域会话'
        _page(request(session, 'GET', target, headers={**nav, 'Sec-Fetch-Dest': 'iframe'}), stage)
    except MabangAuthError:
        raise
    except (requests.RequestException, RuntimeError, ValueError):
        raise MabangAuthError(f'马帮登录失败（{stage}）；原完整凭证保留。') from None


def refresh_login(cred: dict, *, expected: str | None = None) -> None:
    """Serialize password login and save credentials only after a successful renewal."""
    with auth_lock():
        reload_auth(cred)
        if expected is not None and auth_signature(cred) != expected:
            return  # Another process already refreshed the session we were trying to replace.
        baseline = auth_signature(cred)
        candidate = deepcopy(cred)
        with new_session(candidate) as session:
            _password_login(candidate, session)
            result = _complete_session(candidate, session)
        if not commit_auth(cred, result, expected=baseline):
            raise MabangAuthError('马帮凭证在登录期间被其他操作更新，已保留较新的本地配置。')


def _complete_session(candidate: dict, session: requests.Session) -> dict:
    key = cookie_key(cookie_header(session, WWW_BASE))
    aamz = cookie_header(session, AAMZ_BASE)
    if not key or cookie_key(aamz) != key or not cookie_key(aamz, 'PHPSESSID'):
        raise MabangAuthError('马帮未取得同次登录的 www/aamz 凭证；原完整凭证保留。')
    token = exchange_token(candidate, session, key)
    return snapshot({**candidate, 'api_key': key, 'api_bearer': token}, session)


def connect_browser_session(cred: dict, username: str, cookies: list[dict]) -> None:
    """Commit a scoped browser session only for the account configured at startup."""
    if not username or username != (cred.get('login') or {}).get('username'):
        raise MabangAuthError('马帮浏览器账号与本机配置不一致，原凭证保留。')
    expected = auth_signature(cred)
    with auth_lock():
        reload_auth(cred)
        if auth_signature(cred) != expected:
            raise MabangAuthError('马帮凭证在浏览器登录期间已更新，请重新连接。')
        candidate = {**cred, 'cookie_jar': cookies}
        with new_session(candidate) as session:
            for url, mod, data in (
                (WWW_BASE, 'ordera.getForecastLogistics', ''),
                (AAMZ_BASE, 'uploadforecastorderv2.getForecastOrderList',
                 {'searchType': '1', 'status': '1', 'type': '1', 'page': '1', 'rowsPerPage': '1'}),
            ):
                host = 'www.mabangerp.com' if url == WWW_BASE else 'aamz.mabangerp.com'
                try:
                    response = request(session, 'POST', url, headers=headers(candidate, host),
                                       params={'mod': mod}, data=data, follow_redirects=False)
                    _page(response, '会话检查')
                    authenticated = response_json(response).get('success') is True
                except (requests.RequestException, ValueError, RuntimeError):
                    authenticated = False
                if not authenticated:
                    raise MabangAuthError('马帮双域会话未通过只读检查，原凭证保留。')
            result = _complete_session(candidate, session)
        if not commit_auth(cred, result, expected=expected):
            raise MabangAuthError('马帮凭证在浏览器登录期间已更新，请重新连接。')


def refresh_bearer(cred: dict, *, expected: str | None = None) -> None:
    """Renew only API credentials while the current web login key still works."""
    with auth_lock():
        reload_auth(cred)
        if expected is not None and auth_signature(cred) != expected and cred.get('api_bearer'):
            return
        baseline = auth_signature(cred)
        candidate = deepcopy(cred)
        with new_session(candidate) as session:
            key = cookie_key(cookie_header(session, WWW_BASE))
            token = exchange_token(candidate, session, key)
            candidate.update(api_key=key, api_bearer=token)
            if 'cookie_jar' in candidate:
                candidate = snapshot(candidate, session)
        if not commit_auth(cred, candidate, expected=baseline):
            raise MabangAuthError('马帮凭证在换取 Bearer 期间发生变化，已保留新配置。')

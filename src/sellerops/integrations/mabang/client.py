"""Mabang transport: bounded read recovery and no replay of business writes."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from threading import local
from urllib.parse import urlsplit

import requests

from sellerops.integrations.mabang.auth import (
    MabangKeyRejected, api_profile, auto_enabled, refresh_bearer, refresh_login,
)
from sellerops.integrations.mabang.session import (
    AAMZ_BASE, API_BASE, WWW_BASE, MabangAuthError, auth_expired, auth_signature,
    cookie_header, cookie_key, dump_cookies, expires_soon, headers, new_session,
    request, response_json,
)
from sellerops.integrations.mabang.state import commit_auth, reload_auth, snapshot
from sellerops.settings import credentials, paths

REQUEST_INTERVAL = 0.6
DEF_FORECAST_LOGISTICS = '2369||262534'
DEF_FORECAST_CHANNEL = 'wb_box'


@dataclass(repr=False)
class _Connection:
    path: str
    signature: str
    session: requests.Session
    verified: set[str] = field(default_factory=set)
    blocked: MabangAuthError | None = None


_local = local()


def get_mabang_cred() -> dict:
    data = credentials.reload().data.get('mabang') or {}
    if not data.get('shop_map'):
        raise MabangAuthError('credentials.json 缺少 mabang.shop_map（店铺映射不完整）。')
    if not (data.get('www_cookie') or auto_enabled(data)):
        raise MabangAuthError('马帮缺少登录凭证，请运行 mabang-login --from-har <文件>。')
    return data


def api_key_from_www(cred: dict) -> str:
    return cookie_key(cred.get('www_cookie', ''))


def api_ready(cred: dict) -> bool:
    return bool(auto_enabled(cred) or api_key_from_www(cred)
                or (cred.get('api_bearer') and cred.get('api_key')))


def _connection(cred: dict) -> _Connection:
    signature = auth_signature(cred)
    current = getattr(_local, 'connection', None)
    if current and current.path == paths.CREDENTIALS_JSON and current.signature == signature:
        if current.blocked:
            raise current.blocked
        return current
    if current:
        current.session.close()
    current = _Connection(paths.CREDENTIALS_JSON, signature, new_session(cred))
    _local.connection = current
    return current


def _recover(cred: dict, conn: _Connection, *, api: bool) -> _Connection:
    try:
        if api:
            try:
                refresh_bearer(cred, expected=conn.signature)
            except MabangKeyRejected:
                if not auto_enabled(cred):
                    raise
                refresh_login(cred, expected=conn.signature)
        elif auto_enabled(cred):
            refresh_login(cred, expected=conn.signature)
        else:
            raise MabangAuthError('马帮登录会话已失效，请运行 mabang-login --from-har <文件> 更新。')
    except MabangAuthError as exc:
        conn.blocked = exc
        raise
    return _connection(cred)


def _prepare(cred: dict, host: str) -> _Connection:
    if 'login' in cred or 'cookie_jar' in cred:
        reload_auth(cred)
    conn = _connection(cred)
    web_host = 'www.mabangerp.com' if host == 'api.mabangerp.com' else host
    if auto_enabled(cred) and (expires_soon(conn.session, web_host)
            or not cookie_key(cookie_header(conn.session, 'https://' + web_host + '/index.php'))):
        conn = _recover(cred, conn, api=False)
    if host == 'api.mabangerp.com':
        key = cookie_key(cookie_header(conn.session, WWW_BASE))
        if (not cred.get('api_bearer') or (key and cred.get('api_key') and key != cred['api_key'])):
            conn = _recover(cred, conn, api=True)
    return conn


def _record_cookies(cred: dict, conn: _Connection) -> None:
    if 'cookie_jar' not in cred or dump_cookies(conn.session) == cred['cookie_jar']:
        return
    candidate = snapshot(cred, conn.session)
    key = cookie_key(candidate['www_cookie'])
    # Never pair a rotated/removed login key with the previous bearer or aamz session.
    if not key or key != cred.get('api_key') or cookie_key(candidate['aamz_cookie']) != key:
        return
    if commit_auth(cred, candidate, expected=conn.signature):
        conn.signature = auth_signature(cred)


def _send(cred: dict, conn: _Connection, method: str, url: str, *,
          params: dict | None, data: object, timeout: float) -> requests.Response:
    host = urlsplit(url).hostname
    if host == 'api.mabangerp.com':
        key = cookie_key(cookie_header(conn.session, WWW_BASE)) or cred.get('api_key')
        if not key or not cred.get('api_bearer') or (cred.get('api_key') and key != cred['api_key']):
            raise MabangAuthError('马帮 API 缺少匹配的 key 和 Bearer，已停止请求。')
        request_headers = {**api_profile(cred), 'key': key, 'Authorization': 'Bearer ' + cred['api_bearer']}
    else:
        request_headers = headers(cred, host, navigation=method == 'GET')
    return request(conn.session, method, url, headers=request_headers, params=params,
                   data=data, timeout=timeout, follow_redirects=False)


def _checked_response(cred: dict, conn: _Connection, response: requests.Response,
                      host: str, *, read_only: bool) -> requests.Response:
    if response.is_redirect:
        raise RuntimeError('马帮返回了非预期业务跳转，未重放请求。')
    if response.ok:
        if read_only:
            try:
                result = response_json(response)
            except RuntimeError:
                result = {}
            if result.get('success') is True or result.get('code') == 200:
                conn.verified.add(host)
        _record_cookies(cred, conn)
    return response


def _request(cred: dict, method: str, url: str, *, read_only: bool,
             params: dict | None = None, data: object = None, timeout: float = 60) -> requests.Response:
    host = urlsplit(url).hostname
    conn = _prepare(cred, host)
    if not read_only and host != 'api.mabangerp.com' and host not in conn.verified:
        if host == 'www.mabangerp.com':
            probe = form_request(cred, WWW_BASE, 'ordera.getForecastLogistics', read_only=True, data='')
        else:
            probe = form_request(cred, AAMZ_BASE, 'uploadforecastorderv2.getForecastOrderList',
                                 read_only=True, data={'searchType': '1', 'status': '1', 'type': '1',
                                                       'page': '1', 'rowsPerPage': '1'})
        if not probe.ok or response_json(probe).get('success') is not True:
            raise MabangAuthError('马帮写入前未能确认会话可用，未提交业务请求。')
        conn = _connection(cred)
    try:
        response = _send(cred, conn, method, url, params=params, data=data, timeout=timeout)
        if auth_expired(response):
            if not read_only:
                raise MabangAuthError('马帮写入收到会话失效响应，未重放；请核对该笔状态后再处理。')
            conn = _recover(cred, conn, api=host == 'api.mabangerp.com')
            response = _send(cred, conn, method, url, params=params, data=data, timeout=timeout)
            if auth_expired(response):
                raise MabangAuthError('马帮续期后读取仍提示会话失效，已停止。')
        return _checked_response(cred, conn, response, host, read_only=read_only)
    except MabangAuthError as exc:
        conn.blocked = exc
        raise


def form_request(cred: dict, base: str, mod: str, *, read_only: bool, data: object = None,
                 params: dict | None = None, method: str = 'POST', timeout: float = 60) -> requests.Response:
    return _request(cred, method, base, read_only=read_only, params={**(params or {}), 'mod': mod},
                    data=data, timeout=timeout)


def api_post(cred: dict, path: str, body: dict, *, read_only: bool = False) -> requests.Response:
    return _request(cred, 'POST', API_BASE + path, data=json.dumps(body), read_only=read_only)


def parse_json(response: requests.Response) -> dict:
    return response_json(response)

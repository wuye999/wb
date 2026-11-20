"""Extract one successful BCS login from a local HAR without replaying its requests."""
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

from sellerops.core.errors import AuthenticationError
from sellerops.integrations.bcs.capture import (
    BCS_BASE_URL, LOGIN_URL, BCSSession, cookie_fields, login_session,
)


MAX_HAR_BYTES = 64 * 1024 * 1024
_BUSINESS_PATHS = ('/getInfo', '/system/user/profile', '/system/wbShop/user/list')


def _object_json(raw: str | bytes) -> dict:
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _login_body(response: dict) -> dict:
    content = response.get('content', {})
    raw = content.get('text', '{}')
    if content.get('encoding') == 'base64':
        raw = base64.b64decode(raw, validate=True)
    return _object_json(raw)


def _business_session(request: dict, issued: BCSSession) -> BCSSession | None:
    headers = {}
    for item in request.get('headers', []):
        name = item['name'].lower()
        if name in {'authorization', 'x-limit-key', 'cookie'}:
            if name in headers:
                raise ValueError('Duplicate credential header')
            headers[name] = item['value']
    if (headers.get('authorization') != 'Bearer ' + issued.token
            or headers.get('x-limit-key') != issued.limit_key):
        return None
    cookie = headers.get('cookie')
    if cookie is None:
        cookie = '; '.join(f"{item['name']}={item['value']}" for item in request.get('cookies', []))
    cookies = cookie_fields(cookie)
    if cookies.get('Admin-Token') != issued.token or cookies.get('Limit-Key') != issued.limit_key:
        return None
    return login_session(issued.token, issued.limit_key, cookies)


def read_login_har(path: Path, username: str) -> BCSSession:
    """Require a single matching login and a later WB API request with the same credentials."""
    issued = matched = None
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_HAR_BYTES + 1)
        if len(raw) > MAX_HAR_BYTES:
            raise AuthenticationError('BCS HAR 超过 64 MiB，请重新抓取登录和首个业务请求。')
        entries = _object_json(raw)['log']['entries']
        for entry in entries:
            request, response = entry['request'], entry['response']
            if response['status'] != 200:
                continue
            method, url = request['method'], request['url']
            if method == 'POST' and url == LOGIN_URL:
                submitted = _object_json(request.get('postData', {}).get('text', '{}'))
                if submitted.get('username') != username:
                    continue
                body = _login_body(response)
                if body.get('code') != 200:
                    continue
                if issued is not None:
                    raise AuthenticationError('HAR 含多次该账号的成功登录，请只保留一次登录。')
                issued = login_session(body.get('token'), body.get('limitKey'), {})
            elif issued is not None and matched is None and method == 'GET' and url.startswith(BCS_BASE_URL + '/'):
                if urlsplit(url).path in {f'/prod-api{path}' for path in _BUSINESS_PATHS}:
                    matched = _business_session(request, issued)
    except (OSError, KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise AuthenticationError('无法读取有效的 BCS 登录 HAR，请重新导出完整抓包。') from None
    if matched is None:
        raise AuthenticationError('HAR 中找不到与已配置账号及同次登录凭证匹配的 BCS 业务请求。')
    return matched

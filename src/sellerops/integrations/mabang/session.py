"""Scoped cookies, request profiles and bounded HTTP for Mabang authentication."""
from __future__ import annotations

import json
import time
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from sellerops.core.utils import UA

WWW_BASE = 'https://www.mabangerp.com/index.php'
AAMZ_BASE = 'https://aamz.mabangerp.com/index.php'
API_BASE = 'https://api.mabangerp.com/v2'
SSO_GET_TOKEN_URL = 'https://api.mabangerp.com/sso/api/v1/getTokenByKey'
LOGIN_KEY = 'MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE'
HOSTS = {'www.mabangerp.com', 'aamz.mabangerp.com', 'passport.mabangerp.com', 'api.mabangerp.com'}
PROFILE_HEADERS = {
    'user-agent', 'accept-language', 'sec-ch-ua', 'sec-ch-ua-mobile',
    'sec-ch-ua-platform', 'cache-control', 'pragma',
}


class MabangAuthError(RuntimeError):
    """Authentication failed; dependent operations must stop."""


class MabangVerificationRequired(MabangAuthError):
    """The server requires an interactive verification step."""


def verification_error(kind: str) -> MabangVerificationRequired:
    return MabangVerificationRequired(
        f'马帮要求{kind}，已停止自动登录；请人工登录后录制完整 HAR，'
        '运行 mabang-login --from-har <文件> 导入。原完整凭证保留。')


def new_session(cred: dict) -> requests.Session:
    session = requests.Session()
    # Python's default legacy policy otherwise also sends host-only cookies to subdomains.
    session.cookies.set_policy(DefaultCookiePolicy(
        strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    rows = cred.get('cookie_jar')
    if isinstance(rows, list):
        for row in rows:
            domain = str(row.get('domain') or '')
            if domain.lstrip('.') not in HOSTS | {'mabangerp.com'}:
                continue
            cookie = requests.cookies.create_cookie(
                str(row['name']), str(row['value']), domain=domain,
                path=row.get('path') or '/', secure=bool(row.get('secure')),
                expires=row.get('expires'),
                rest={'HttpOnly': row.get('httpOnly', False), 'SameSite': row.get('sameSite', '')})
            cookie.domain_specified = not row.get('hostOnly', not domain.startswith('.'))
            cookie.domain_initial_dot = domain.startswith('.')
            session.cookies.set_cookie(cookie)
    else:
        # Old flat strings have no scope metadata: keep each string strictly on its own host.
        for field, host in [('www_cookie', 'www.mabangerp.com'), ('aamz_cookie', 'aamz.mabangerp.com')]:
            raw = cred.get(field) or (cred.get('www_cookie', '') if field == 'aamz_cookie' else '')
            for item in raw.split(';'):
                name, sep, value = item.strip().partition('=')
                if sep and name:
                    cookie = requests.cookies.create_cookie(name, value, domain=host, path='/')
                    cookie.domain_specified = False
                    session.cookies.set_cookie(cookie)
    return session


def dump_cookies(session: requests.Session) -> list[dict]:
    rows = []
    for c in session.cookies:
        if c.is_expired():
            continue
        rest = {k.lower(): v for k, v in c._rest.items()}
        rows.append(dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
                         expires=c.expires, secure=c.secure, hostOnly=not c.domain_specified,
                         httpOnly=rest.get('httponly', False) is not False, sameSite=rest.get('samesite', '')))
    return rows


def cookie_header(session: requests.Session, url: str) -> str:
    prepared = requests.Request('GET', url).prepare()
    return requests.cookies.get_cookie_header(session.cookies, prepared) or ''


def cookie_key(raw: str, name: str = LOGIN_KEY) -> str:
    values = {part.strip().partition('=')[2] for part in raw.split(';')
              if part.strip().partition('=')[0] == name and '=' in part}
    if len(values) > 1:
        raise MabangAuthError('马帮同域登录 Cookie 冲突，请重新导入完整登录 HAR。')
    return next(iter(values), '')


def headers(cred: dict, host: str, *, navigation: bool = False, login: bool = False) -> dict:
    profile = (cred.get('login') or {}).get('headers') or {}
    result = {k: str(v) for k, v in profile.items() if k.lower() in PROFILE_HEADERS}
    result.setdefault('user-agent', UA)
    result.setdefault('accept-language', 'zh-CN,zh;q=0.9')
    if navigation:
        result.update({'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                       'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
                       'Sec-Fetch-Site': 'same-site'})
    else:
        referer = 'https://www.mabangerp.com/index.htm' if login else (
            'https://aamz.mabangerp.com/index.php?mod=uploadforecastorderv2.list'
            if host == 'aamz.mabangerp.com' else WWW_BASE + '?mod=order.list')
        result.update({'Accept': 'application/json, text/javascript, */*; q=0.01',
                       'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                       'Origin': 'https://' + host, 'Referer': referer,
                       'X-Requested-With': 'XMLHttpRequest', 'Sec-Fetch-Dest': 'empty',
                       'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin'})
    return result


def request(session: requests.Session, method: str, url: str, *, headers: dict,
            data: object = None, params: dict | None = None, timeout: float = 30,
            follow_redirects: bool = True) -> requests.Response:
    """Follow a bounded Mabang-only chain without leaking host cookies or signed URLs."""
    current_headers = dict(headers)
    for _ in range(11):
        address = urlsplit(url)
        if (address.scheme != 'https' or address.hostname not in HOSTS or address.port not in (None, 443)
                or address.username or address.password):
            raise MabangAuthError('马帮返回了非预期跳转地址，已停止请求。')
        prepared = session.prepare_request(requests.Request(
            method, url, headers=current_headers, data=data, params=params))
        # Session.prepare_request copies cookies into a jar with a permissive default policy.
        prepared.headers.pop('Cookie', None)
        scoped = cookie_header(session, prepared.url)
        if scoped:
            prepared.headers['Cookie'] = scoped
        try:
            response = session.send(prepared, timeout=timeout, allow_redirects=False)
        except requests.Timeout:
            raise requests.Timeout('马帮请求超时；未自动重放请求。') from None
        except requests.RequestException:
            raise requests.ConnectionError('马帮网络请求失败；未自动重放请求。') from None
        if not follow_redirects or response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get('Location')
        if not location:
            raise MabangAuthError('马帮跳转缺少目标地址。')
        next_url = urljoin(prepared.url, location)
        cross_host = urlsplit(next_url).hostname != address.hostname
        if method.upper() != 'GET' and response.status_code in (307, 308):
            raise MabangAuthError('马帮要求重发请求正文，已停止。')
        if response.status_code == 303 or (response.status_code in (301, 302) and method.upper() == 'POST'):
            method, data = 'GET', None
            current_headers = {k: v for k, v in current_headers.items()
                               if k.lower() in PROFILE_HEADERS}
            current_headers['Accept'] = 'text/html,application/xhtml+xml,*/*;q=0.8'
        if cross_host:
            current_headers = {k: v for k, v in current_headers.items()
                               if k.lower() not in {'authorization', 'key', 'origin', 'referer'}}
        url, params = next_url, None
    raise MabangAuthError('马帮登录跳转超过安全上限。')


def response_json(response: requests.Response) -> dict:
    try:
        value = response.json()
    except (ValueError, TypeError):
        text = response.text
        if not isinstance(text, str) or not text.startswith('\ufeff'):
            raise RuntimeError('马帮返回非 JSON 响应，不能当作空数据继续。') from None
        try:
            value = json.loads(text.lstrip('\ufeff'))
        except ValueError:
            raise RuntimeError('马帮返回非 JSON 响应，不能当作空数据继续。') from None
    if not isinstance(value, dict):
        raise RuntimeError('马帮返回的 JSON 层级无效。')
    return value


def auth_expired(response: requests.Response) -> bool:
    try:
        value = response_json(response)
    except RuntimeError:
        value = {}
    if isinstance(value, dict):
        code = value.get('errCode')
        if code == 'CHECK_VERIFY_CODE':
            raise verification_error('腾讯验证码')
        if str(code) == '2001':
            raise verification_error('短信验证')
        message = str(value.get('message') or value.get('msg') or '').lower()
        if value.get('success') is not True and any(s in message for s in (
                '登录信息已过期', '登录信息已超时', '请先登录', '未登录', '登录已过期',
                '登录已失效', 'token expired', 'token has expired', 'unauthorized')):
            return True
        if str(value.get('code')) == '401':
            return True
    if response.status_code == 401:
        return True
    mod = parse_qs(urlsplit(response.url or '').query).get('mod', [''])[0]
    if mod == 'main.login':
        return True
    if response.is_redirect:
        target = urlsplit(urljoin(response.url or WWW_BASE, response.headers['Location']))
        if target.hostname in HOSTS and (target.path == '/index.htm'
                or parse_qs(target.query).get('mod') == ['main.login']):
            return True
    text = response.text[:350000]
    return ('main.doLogin' in text and 'login111' in text
            and ('name="password"' in text or "name='password'" in text))


def expires_soon(session: requests.Session, host: str) -> bool:
    names = {LOGIN_KEY, 'MABANG_ERP_PRO_MEMBERINFO_LOGIN_PLUS', 'PHPSESSID'}
    now = time.time()
    return any(c.name in names and c.domain.lstrip('.') == host
               and c.expires is not None and c.expires <= now + 300 for c in session.cookies)


def auth_signature(cred: dict) -> str:
    return json.dumps({k: cred.get(k) for k in AUTH_FIELDS}, sort_keys=True, ensure_ascii=False)


AUTH_FIELDS = ('login', 'cookie_jar', 'www_cookie', 'aamz_cookie', 'api_key', 'api_bearer', 'auth_updated_at')

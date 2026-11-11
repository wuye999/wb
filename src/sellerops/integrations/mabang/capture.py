"""Read a successful Mabang login and forecast-page session from a local HAR."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from sellerops.integrations.mabang.session import (
    AAMZ_BASE, HOSTS, LOGIN_KEY, PROFILE_HEADERS, WWW_BASE, MabangAuthError,
    cookie_header, cookie_key, dump_cookies, new_session,
)


def _body(entry: dict) -> str:
    content = entry['response'].get('content') or {}
    text = content.get('text') or ''
    if content.get('encoding') == 'base64':
        return base64.b64decode(text).decode('utf-8', errors='replace')
    return text


def _success(entry: dict) -> bool:
    try:
        value = json.loads(_body(entry).lstrip('\ufeff'))
    except ValueError:
        return False
    return isinstance(value, dict) and value.get('success') is True


def _matches(entry: dict, host: str, mod: str, method: str) -> bool:
    req = entry['request']
    address = urlsplit(req['url'])
    return (address.scheme == 'https' and address.hostname == host and address.path == '/index.php'
            and req['method'] == method and parse_qs(address.query).get('mod') == [mod])


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


def forecast_frame(html: str) -> str:
    parser = _ForecastFrame()
    parser.feed(html)
    if len(parser.urls) != 1:
        raise MabangAuthError('未在马帮页面找到唯一的上传预报入口。')
    return next(iter(parser.urls))


def _timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
    except ValueError:
        return int(parsedate_to_datetime(value).timestamp())


def _put(jar: requests.cookies.RequestsCookieJar, host: str, row: dict) -> None:
    domain = str(row.get('domain') or host).lower()
    domain_base = domain.lstrip('.')
    if domain_base not in HOSTS | {'mabangerp.com'} or not (
            host == domain_base or host.endswith('.' + domain_base)):
        return
    cookie = requests.cookies.create_cookie(
        row['name'], row.get('value', ''), domain=domain, path=row.get('path') or '/',
        expires=_timestamp(row.get('expires')), secure=bool(row.get('secure')),
        rest={'HttpOnly': bool(row.get('httpOnly')), 'SameSite': row.get('sameSite') or ''})
    cookie.domain_specified = not row.get('hostOnly', not domain.startswith('.'))
    cookie.domain_initial_dot = domain.startswith('.')
    jar.set_cookie(cookie)


def _collect(entry: dict, jar: requests.cookies.RequestsCookieJar) -> None:
    req = entry['request']
    url = urlsplit(req['url'])
    if url.scheme != 'https' or url.hostname not in HOSTS:
        return
    host = url.hostname
    for row in req.get('cookies') or []:
        _put(jar, host, row)
    resp = entry['response']
    # Chrome records request cookies with scope, and headers retain response deletions.
    for h in resp.get('headers') or []:
        if h['name'].lower() != 'set-cookie':
            continue
        parsed = SimpleCookie()
        parsed.load(h['value'])
        for name, morsel in parsed.items():
            domain = morsel['domain']
            domain = ('.' + domain.lstrip('.')) if domain else host
            expires = morsel['expires']
            if morsel['max-age']:
                origin = _timestamp(entry.get('startedDateTime'))
                if origin is None:
                    raise MabangAuthError('HAR Cookie 缺少计算到期时间所需的请求时间。')
                expires = datetime.fromtimestamp(origin + int(morsel['max-age'])).astimezone().isoformat()
            _put(jar, host, dict(name=name, value=morsel.value, domain=domain,
                                 hostOnly=not morsel['domain'], path=morsel['path'] or url.path.rpartition('/')[0] or '/',
                                 expires=expires, secure=bool(morsel['secure']),
                                 httpOnly=bool(morsel['httponly']), sameSite=morsel['samesite']))


def read_har(path: Path) -> dict:
    """Return validated login configuration and the scoped cookies of one successful chain."""
    try:
        entries = json.loads(path.read_text(encoding='utf-8-sig'))['log']['entries']
        starts = [i for i, e in enumerate(entries)
                  if _matches(e, 'www.mabangerp.com', 'main.doLogin', 'POST') and _success(e)]
        if len(starts) != 1:
            raise MabangAuthError('HAR 必须包含一条明确成功的马帮密码登录。')
        start = starts[0]
        login = entries[start]['request']
        post = login.get('postData') or {}
        if not (post.get('text') and 'application/x-www-form-urlencoded' in post.get('mimeType', '')):
            raise MabangAuthError('HAR 缺少登录的原始表单正文，请导出含敏感数据和响应内容的本地 HAR。')
        form = parse_qs(post['text'], keep_blank_values=True)
        if any(len(form.get(k, [])) != 1 or not form[k][0] for k in ('username', 'password')):
            raise MabangAuthError('HAR 未包含唯一的马帮账号和密码。')
        menu = next(i for i in range(start + 1, len(entries))
                    if _matches(entries[i], 'www.mabangerp.com', 'main.gotoApp', 'GET')
                    and parse_qs(urlsplit(entries[i]['request']['url']).query).get('menuKey') == ['M0010212'])
        frame = forecast_frame(_body(entries[menu]))
        enter = next(i for i in range(menu + 1, len(entries))
                     if entries[i]['request']['url'] == frame
                     and entries[i]['request']['method'] == 'GET'
                     and entries[i]['response']['status'] == 200)
        finish = next(i for i in range(enter + 1, len(entries))
                      if _matches(entries[i], 'aamz.mabangerp.com', 'uploadforecastorderv2.getForecastOrderList', 'POST')
                      and _success(entries[i]))
        with new_session({}) as session:
            for entry in entries[start:finish + 1]:
                _collect(entry, session.cookies)
            www_cookie, aamz_cookie = cookie_header(session, WWW_BASE), cookie_header(session, AAMZ_BASE)
            key = cookie_key(www_cookie)
            if not key or cookie_key(aamz_cookie) != key or not cookie_key(aamz_cookie, 'PHPSESSID'):
                raise MabangAuthError('HAR 缺少仍有效且属于同次登录的 www/aamz 凭证，请重新抓取。')
            rows = dump_cookies(session)
        profile = {h['name'].lower(): h['value'] for h in login.get('headers') or []
                   if h['name'].lower() in PROFILE_HEADERS}
        return dict(www_cookie=www_cookie, aamz_cookie=aamz_cookie, cookie_jar=rows,
                    login=dict(username=form['username'][0], password=form['password'][0],
                               timezone=form.get('timezone', ['UTC+8'])[0], headers=profile, auto_refresh=True,
                               passport_sso=any(_matches(e, 'www.mabangerp.com', 'main.plogin', 'GET')
                                                for e in entries[start + 1:menu])))
    except MabangAuthError:
        raise
    except (OSError, ValueError, KeyError, TypeError, StopIteration, OverflowError):
        raise MabangAuthError('HAR 无法读取或缺少成功登录、预报入口及列表响应的完整链路。') from None

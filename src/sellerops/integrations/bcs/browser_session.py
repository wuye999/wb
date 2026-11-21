"""Read the BCS business session from its dedicated browser profile."""
import re
from urllib.parse import urlencode

from sellerops.core.errors import AuthenticationError
from sellerops.integrations.bcs import auth
from sellerops.integrations.bcs.session import BCSSession, cookie_fields, login_session
from sellerops.integrations.sessions import browser
from sellerops.integrations.sessions.browser import url_without_query


HOST = 'wb.bcserp.com'
LOGIN_ENTRY = auth.LOGIN_PAGE + '?' + urlencode({'redirect': 'https://wb.bcserp.com/index'})
NAVIGATION_TIMEOUT_MS = 25_000
_READS = {auth.PROFILE_URL, auth.BCS_BASE_URL + '/getInfo', auth.BCS_BASE_URL + '/system/wbShop/user/list'}


def is_logged_in(page) -> bool:
    return browser.is_site(page.url, HOST)


def read_session(context) -> BCSSession | None:
    page = browser.platform_page(context, HOST)
    reads = []

    def record(response) -> None:
        if (response.ok and response.request.method == 'GET'
                and url_without_query(response.url) in _READS and len(reads) < 20):
            reads.append(response.request)

    page.on('response', record)
    try:
        page.goto(auth.BUSINESS_HOME, wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)
        from playwright.sync_api import TimeoutError

        try:
            page.wait_for_url(lambda url: browser.is_site(url, HOST), timeout=2000)
        except TimeoutError:
            return None
        if not reads:
            try:
                page.wait_for_event('response', predicate=lambda r: url_without_query(r.url) in _READS, timeout=3500)
            except TimeoutError:
                pass  # The auth cookies are checked by the same read-only account probe.
        if not browser.is_site(page.url, HOST):
            return None
        try:
            for req in reversed(reads):
                bearer = re.fullmatch(r'Bearer (\S+)', req.header_value('authorization') or '', re.I)
                limit = req.header_value('x-limit-key')
                if bearer and limit:
                    raw = req.header_value('cookie') or browser.cookie_header(context, req.url)
                    cookies = cookie_fields(raw)
                    if cookies.get('Admin-Token') != bearer[1] or cookies.get('Limit-Key') != limit:
                        raise AuthenticationError('BCS 浏览器认证头与 Cookie 不一致，原凭证保留。')
                    return login_session(bearer[1], limit, cookies)
            cookies = cookie_fields(browser.cookie_header(context, auth.PROFILE_URL))
            if cookies.get('Admin-Token') and cookies.get('Limit-Key'):
                return login_session(cookies['Admin-Token'], cookies['Limit-Key'], cookies)
        except ValueError:
            raise AuthenticationError('BCS 浏览器会话格式无效，原凭证保留。') from None
        return None
    finally:
        page.remove_listener('response', record)

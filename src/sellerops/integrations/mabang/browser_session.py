"""Read the two-domain Mabang session and account from its browser profile."""
from dataclasses import dataclass, field

from sellerops.integrations.mabang.session import AAMZ_BASE, LOGIN_KEY, WWW_BASE
from sellerops.integrations.sessions import browser
from sellerops.integrations.sessions.browser import url_without_query


HOST = 'www.mabangerp.com'
LOGIN_ENTRY = WWW_BASE + '?mod=order.list&Order_orderStatus=2'
FORECAST_PAGE = WWW_BASE + '?mod=main.gotoApp&v=v3&menuKey=M0010212'
NAVIGATION_TIMEOUT_MS = 25_000
_ACCOUNT_SELECTOR = '#mb-user > span[title]'


def is_logged_in(page) -> bool:
    return browser.is_site(page.url, HOST) and page.locator(_ACCOUNT_SELECTOR).count() == 1


@dataclass(frozen=True)
class MabangSession:
    username: str = field(repr=False)
    cookies: list[dict] = field(repr=False)


def read_session(context) -> MabangSession | None:
    from playwright.sync_api import TimeoutError

    page = browser.platform_page(context, HOST)
    response = page.goto(FORECAST_PAGE, wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)
    if (not response or not response.ok or not browser.is_site(page.url, HOST)
            or url_without_query(page.url) != WWW_BASE):
        return None
    # The authenticated forecast page's global account menu contains the login name.
    identity = page.locator(_ACCOUNT_SELECTOR)
    try:
        identity.wait_for(state='attached', timeout=3500)
    except TimeoutError:
        return None
    username = (identity.get_attribute('title', timeout=3500) or '').strip()
    if not any(row['name'] == 'PHPSESSID' for row in context.cookies([AAMZ_BASE])):
        try:
            page.wait_for_event('response', predicate=lambda r: r.ok and url_without_query(r.url) == AAMZ_BASE,
                                timeout=3500)
        except TimeoutError:
            return None
    www = context.cookies([WWW_BASE])
    aamz = context.cookies([AAMZ_BASE])
    keys = {row['value'] for row in www if row['name'] == LOGIN_KEY and row['value']}
    if (not username or len(keys) != 1 or keys != {row['value'] for row in aamz if row['name'] == LOGIN_KEY}
            or not any(row['name'] == 'PHPSESSID' and row['value'] for row in aamz)):
        return None
    rows = {(row['name'], row['domain'], row['path']): row for row in www + aamz}
    cookies = [{**row, 'expires': int(row['expires']) if row['expires'] > 0 else None,
                'hostOnly': not row['domain'].startswith('.')} for row in rows.values()]
    return MabangSession(username, cookies)

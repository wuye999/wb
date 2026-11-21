"""Read the WB account and store list from its dedicated browser profile."""
from dataclasses import dataclass, field
from urllib.parse import urlencode

from sellerops.core.errors import AuthenticationError
from sellerops.integrations.sessions import browser
from sellerops.integrations.sessions.browser import url_without_query
from sellerops.integrations.wildberries.session_update import account_id, shop_cookie, shop_uuid


HOST = 'seller.wildberries.ru'
AUTH_HOST = 'seller-auth.wildberries.ru'
HOME = f'https://{HOST}/'
LOGIN_ENTRY = f'https://{AUTH_HOST}/zh/?' + urlencode({'redirect_url': HOME + 'card-main/all-goods'})
SUPPLIERS_URL = HOME + 'ns/suppliers/suppliers-portal-core/suppliers'
NAVIGATION_TIMEOUT_MS = 180_000
SHOPS_TIMEOUT_MS = 240_000


def is_logged_in(page) -> bool:
    return browser.is_site(page.url, HOST)


@dataclass(frozen=True)
class WBShop:
    uuid: str
    old_id: int
    name: str


@dataclass(frozen=True)
class WBSession:
    authorize: str = field(repr=False)
    cookie: str = field(repr=False)
    shops: tuple[WBShop, ...]


def _supplier_call(request) -> dict | None:
    if request.method != 'POST' or url_without_query(request.url) != SUPPLIERS_URL:
        return None
    try:
        data = request.post_data_json
    except (TypeError, ValueError):
        return None
    calls = data if isinstance(data, list) else [data]
    matches = [row for row in calls if isinstance(row, dict) and row.get('method') == 'getUserSuppliers']
    return matches[0] if len(matches) == 1 else None


def parse_wb_shops(body, call_id) -> tuple[WBShop, ...]:
    responses = body if isinstance(body, list) else [body]
    matches = [row for row in responses if isinstance(row, dict) and row.get('id') == call_id]
    if len(matches) != 1 or matches[0].get('error'):
        raise AuthenticationError('WB 未返回唯一的店铺列表，原凭证保留。')
    result = matches[0].get('result')
    rows = result.get('suppliers') if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise AuthenticationError('WB 店铺列表响应不完整，原凭证保留。')
    shops = []
    seen = set()
    for row in rows:
        uuid = shop_uuid(row.get('id')) if isinstance(row, dict) else ''
        if not uuid or uuid in seen:
            raise AuthenticationError('WB 店铺身份缺失或重复，原凭证保留。')
        seen.add(uuid)
        if row.get('isDeleted') is True or row.get('deactivated') is True:
            continue
        old_id, name = row.get('oldID'), row.get('name')
        if type(old_id) is not int or not isinstance(name, str) or not name.strip():
            raise AuthenticationError('WB 店铺列表缺少店铺名称或编号，原凭证保留。')
        shops.append(WBShop(uuid, old_id, ' '.join(name.split())))
    return tuple(shops)


def read_session(context) -> WBSession | None:
    from playwright.sync_api import TimeoutError

    page = browser.platform_page(context, HOST)

    def match_response(response) -> bool:
        if browser.is_site(response.url, AUTH_HOST):
            return response.request.resource_type == 'document' and response.frame == page.main_frame
        return response.ok and _supplier_call(response.request) is not None

    try:
        with page.expect_response(match_response, timeout=SHOPS_TIMEOUT_MS) as pending:
            page.goto(HOME, wait_until='commit', timeout=NAVIGATION_TIMEOUT_MS)
        response = pending.value
    except TimeoutError:
        return None
    if browser.is_site(response.url, AUTH_HOST) or not is_logged_in(page):
        return None
    request = response.request
    authorize = request.header_value('authorizev3') or ''
    if not account_id(authorize):
        return None
    try:
        shops = parse_wb_shops(response.json(), _supplier_call(request)['id'])
        if not shops:
            # A logged-in account without a usable shop may still send a placeholder shop cookie.
            return WBSession(authorize, '', shops)
        cookie = request.header_value('cookie') or browser.cookie_header(context, request.url)
        supplier, _ = shop_cookie(cookie)
    except (KeyError, TypeError, ValueError):
        raise AuthenticationError('WB 浏览器店铺响应无效，原凭证保留。') from None
    if supplier not in {shop.uuid for shop in shops}:
        raise AuthenticationError('请在 WB 卖家后台选中要连接的店铺后重试，原凭证保留。')
    return WBSession(authorize, cookie, shops)

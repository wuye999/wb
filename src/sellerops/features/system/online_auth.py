"""使用现有会话进行一次性只读检查，凭证不进入结果。"""
from collections import Counter

from sellerops.core.errors import AuthenticationError
from sellerops.integrations.bcs.client import BCSClient
from sellerops.integrations.bcs.session import BCS_BASE_URL, BCSSession
from sellerops.integrations.feishu import tables
from sellerops.integrations.mabang import session as mabang
from sellerops.integrations.wildberries.auth import probe_session
from sellerops.settings.credentials import wb_credential_warnings, wb_session_configured, wb_shop_id, wb_template_shop


def check_bcs(cred) -> str:
    if not (cred.token and cred.limit_key):
        return 'missing'
    if cred.base_url != BCS_BASE_URL:
        return 'unavailable'
    session = BCSSession(cred.token, cred.limit_key, cred.bcs_headers()['Cookie'])
    client = BCSClient(session, auto_refresh=False)
    result = client.get(client.base_url + '/system/wbShop/user/list?pageNum=1&pageSize=1',
                        retry=1)
    if result.get('code') != 200 or not isinstance(result.get('rows'), list):
        raise AuthenticationError('BCS 会话不可用')
    return 'verified'


def check_wb(cred) -> dict | str:
    rows = cred.wb.get('shops', [])
    if not isinstance(rows, list):
        return 'unavailable'
    shops = [shop for shop in rows if wb_shop_id(shop) is not None]
    counts = Counter(shop['shopId'] for shop in shops)
    conflicts = {shop_id for shop_id, count in counts.items() if count > 1}
    for warning in wb_credential_warnings(shops):
        if warning['code'] != 'legacy_wb_template_shop':
            conflicts.update(warning['shop_ids'])

    checks = {}
    for shop in shops:
        if wb_template_shop(shop):
            continue
        shop_id = shop['shopId']
        sid = str(shop_id)
        if shop_id in conflicts:
            checks[sid] = 'unavailable'
            continue
        if not wb_session_configured(shop):
            checks[sid] = 'missing'
            continue
        try:
            probe_session(shop, cred.root_version)
            checks[sid] = 'verified'
        except AuthenticationError:
            checks[sid] = 'authentication_failed'
        except Exception:
            checks[sid] = 'unavailable'
    return checks or 'missing'


def check_mabang(cred) -> str:
    current = cred.data.get('mabang') or {}
    with mabang.new_session(current) as session:
        if not mabang.cookie_header(session, mabang.WWW_BASE):
            return 'missing'
        response = mabang.request(session, 'GET', mabang.WWW_BASE,
                                  params={'mod': 'main.gotoApp', 'v': 'v3', 'menuKey': 'M0010212'},
                                  headers=mabang.headers(current, 'www.mabangerp.com', navigation=True),
                                  timeout=10)
        if mabang.auth_expired(response):
            raise AuthenticationError('马帮会话不可用')
        response.raise_for_status()
        if 'uploadforecastorderv2.list' not in response.text:
            return 'unavailable'
    return 'verified'


def check_feishu(cred) -> str:
    if not cred.feishu_base_url():
        return 'missing'
    token = tables.resolve_base(cred.feishu_base_url())
    response = tables.invoke(['+table-list', '--base-token', token], timeout=15)
    return 'verified' if isinstance(response.get('tables'), list) else 'unavailable'

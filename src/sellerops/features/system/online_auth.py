"""使用现有会话进行一次性只读检查，凭证不进入结果。"""
from sellerops.integrations.bcs.client import BCSClient
from sellerops.integrations.bcs.session import BCSSession
from sellerops.integrations.feishu import tables
from sellerops.integrations.mabang import session as mabang
from sellerops.integrations.wildberries.auth import probe_session
from sellerops.core.errors import AuthenticationError


def check_bcs(cred) -> str:
    if not (cred.token and cred.limit_key):
        return 'missing'
    session = BCSSession(cred.token, cred.limit_key, cred.bcs_headers()['Cookie'])
    client = BCSClient(session)
    result = client.get(client.base_url + '/system/wbShop/user/list?pageNum=1&pageSize=1',
                        retry=1)
    if result.get('code') != 200 or not isinstance(result.get('rows'), list):
        raise AuthenticationError('BCS 会话不可用')
    return 'verified'


def check_wb(cred) -> dict | str:
    checks = {}
    for shop in cred.wb.get('shops', []):
        sid = shop.get('shopId')
        if not sid or not all(shop.get(k) for k in ('authorizev3', 'wb_seller_lk', 'cookie')):
            checks[str(sid)] = 'missing'
            continue
        try:
            probe_session(shop, cred.root_version)
            checks[str(sid)] = 'verified'
        except AuthenticationError:
            checks[str(sid)] = 'authentication_failed'
        except Exception:
            checks[str(sid)] = 'unavailable'
    return checks or 'missing'


def check_mabang(cred) -> str:
    current = cred.data.get('mabang') or {}
    if not (current.get('www_cookie') or current.get('cookies')):
        return 'missing'
    with mabang.new_session(current) as session:
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

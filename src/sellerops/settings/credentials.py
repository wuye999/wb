"""
sellerops 统一凭证管理

单一凭证文件：data/credentials.json，结构：
{
  "bcs": { "base_url": "...", "token": "...", "limit_key": "...", "cookie_extra": "..." },
  "wb":  { "root_version": "v1.108.1", "shops": [ {shop_name, shop_id, authorizev3, wb_seller_lk, cookie}, ... ] }
}

BCS 三件套（token/limit_key/cookie_extra）+ WB 全部店铺 cookie 全在这里，换凭证只改一个文件。
"""


from collections import defaultdict
from functools import lru_cache
import json
import re

from sellerops.core.errors import ValidationError
from sellerops.core.files import safe_load_json
from sellerops.domain.mabang_warehouse import warehouse_config
from sellerops.domain.wb_identity import shop_cookie, token_shop_uuid
from sellerops.settings import paths as config


DEFAULT_ROOT_VERSION = "v1.108.1"
DEFAULT_BCS_URL = 'https://wb.bcserp.com/prod-api'
WB_SESSION_KEYS = ('authorizev3', 'wb_seller_lk', 'cookie')
_WB_TEMPLATE_FIELDS = {'shopId', 'shopName', *WB_SESSION_KEYS}
_MABANG_TEMPLATE_ENTRY = ('马帮中的店铺标识', '示例店铺(1)')


def _configured(section: dict, key: str) -> bool:
    value = section.get(key)
    return isinstance(value, str) and bool(value.strip())


def wb_shop_id(shop: object) -> int | None:
    shop_id = shop.get('shopId') if isinstance(shop, dict) else None
    return shop_id if type(shop_id) is int and shop_id > 0 else None


def wb_session_configured(shop: dict) -> bool:
    return all(_configured(shop, key) for key in WB_SESSION_KEYS)


def wb_template_shop(shop: dict) -> bool:
    return (wb_shop_id(shop) == 1 and shop.get('shopName') == '示例店铺'
            and shop.keys() <= _WB_TEMPLATE_FIELDS
            and all(isinstance(shop.get(key, ''), str) and not _configured(shop, key) for key in WB_SESSION_KEYS))


def wb_credential_warnings(shops: list) -> list[dict]:
    warnings = []
    suppliers = defaultdict(set)
    seller_tokens = defaultdict(set)
    for shop in shops:
        shop_id = wb_shop_id(shop)
        if shop_id is None:
            continue
        if wb_template_shop(shop):
            if not warnings:
                warnings.append({'path': 'wb.shops', 'code': 'legacy_wb_template_shop', 'shop_ids': [shop_id]})
            continue
        token = shop.get('wb_seller_lk')
        if _configured(shop, 'wb_seller_lk'):
            seller_tokens[token].add(shop_id)
        cookie = shop.get('cookie')
        cookie_supplier = ''
        if isinstance(cookie, str):
            try:
                cookie_supplier, _ = shop_cookie(cookie)
            except ValidationError:
                pass
        for supplier in (cookie_supplier, token_shop_uuid(token)):
            if supplier:
                suppliers[supplier].add(shop_id)
    for groups, code in ((suppliers, 'duplicate_wb_supplier_uuid'), (seller_tokens, 'duplicate_wb_seller_lk')):
        affected = {tuple(sorted(ids)) for ids in groups.values() if len(ids) > 1}
        warnings.extend({'path': 'wb.shops', 'code': code, 'shop_ids': list(ids)} for ids in sorted(affected))
    return warnings


def _invalid_json_constant(_value: str):
    raise ValueError('non-JSON numeric constant')


def inspect_credentials(path) -> dict:
    """Check the local structure and project configuration flags without copying credentials."""
    diagnosis = {'status': 'missing', 'json_valid': None, 'structure_valid': None, 'issues': [], 'warnings': []}
    issues = diagnosis['issues']
    warnings = diagnosis['warnings']
    missing = raw = object()
    try:
        with open(path, encoding='utf-8-sig') as stream:
            raw = json.load(stream, parse_constant=_invalid_json_constant)
    except FileNotFoundError:
        pass
    except (UnicodeError, ValueError):
        diagnosis.update(status='invalid_json', json_valid=False)
    except OSError:
        diagnosis['status'] = 'unreadable'

    def object_section(parent: dict, key: str, location: str) -> dict:
        value = parent.get(key, {})
        if not isinstance(value, dict):
            issues.append({'path': location, 'code': 'expected_object'})
            return {}
        return value

    def check_strings(section: dict, location: str, keys: tuple[str, ...]) -> None:
        for key in keys:
            if key in section and not isinstance(section[key], str):
                issues.append({'path': f'{location}.{key}', 'code': 'expected_string'})

    parsed = raw is not missing
    data = raw if isinstance(raw, dict) else {}
    if parsed and not isinstance(raw, dict):
        issues.append({'path': '$', 'code': 'expected_object'})
    bcs = object_section(data, 'bcs', 'bcs')
    wb = object_section(data, 'wb', 'wb')
    mabang = object_section(data, 'mabang', 'mabang')
    feishu = object_section(data, 'feishu', 'feishu')
    object_section(data, 'ai', 'ai')
    bcs_login = object_section(bcs, 'login', 'bcs.login')
    mabang_login = object_section(mabang, 'login', 'mabang.login')
    check_strings(bcs, 'bcs', ('base_url', 'token', 'limit_key', 'cookie_extra'))
    check_strings(wb, 'wb', ('warehouse_keyword', 'root_version'))
    check_strings(mabang, 'mabang', ('www_cookie', 'aamz_cookie', 'api_bearer', 'api_key'))
    check_strings(feishu, 'feishu', ('base_url',))
    check_strings(bcs_login, 'bcs.login', ('username', 'password'))
    check_strings(mabang_login, 'mabang.login', ('username', 'password'))
    shop_map = object_section(mabang, 'shop_map', 'mabang.shop_map')
    warehouse = warehouse_config(mabang)
    issues.extend({'path': f'mabang.{field}', 'code': code} for field, code in warehouse.issues)
    rows = wb.get('shops', [])
    seen, ids, complete = set(), set(), set()
    if not isinstance(rows, list):
        issues.append({'path': 'wb.shops', 'code': 'expected_array'})
    else:
        for index, row in enumerate(rows, start=1):
            location = f'wb.shops[{index}]'
            if not isinstance(row, dict):
                issues.append({'path': location, 'code': 'expected_object'})
                continue
            shop_id = wb_shop_id(row)
            if shop_id is None:
                issues.append({'path': f'{location}.shopId', 'code': 'invalid_shop_id'})
                continue
            if shop_id in seen:
                issues.append({'path': f'{location}.shopId', 'code': 'duplicate_shop_id'})
            seen.add(shop_id)
            check_strings(row, location, ('shopName', *WB_SESSION_KEYS))
            if wb_template_shop(row):
                continue
            ids.add(shop_id)
            if wb_session_configured(row):
                complete.add(shop_id)
        warnings.extend(wb_credential_warnings(rows))

    if shop_map.get(_MABANG_TEMPLATE_ENTRY[0]) == _MABANG_TEMPLATE_ENTRY[1]:
        warnings.append({'path': 'mabang.shop_map', 'code': 'legacy_mabang_template_shop_map'})
    mapped_shops = sum(bool(key.strip()) and isinstance(value, str) and bool(value.strip())
                       for key, value in shop_map.items() if (key, value) != _MABANG_TEMPLATE_ENTRY)
    counts_known = ((isinstance(raw, dict) and isinstance(data.get('wb', {}), dict) and isinstance(rows, list))
                    or diagnosis['status'] == 'missing')
    root = wb.get('root_version', DEFAULT_ROOT_VERSION if 'wb' in data else None)
    root_version = root if isinstance(root, str) and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', root) else None
    bcs_default = bcs.get('base_url', DEFAULT_BCS_URL) == DEFAULT_BCS_URL
    bcs_user = _configured(bcs_login, 'username')
    bcs_password = _configured(bcs_login, 'password')
    bcs_refresh = bcs_login.get('auto_refresh', True) is True
    bcs_token = _configured(bcs, 'token')
    bcs_limit_key = _configured(bcs, 'limit_key')
    wb_warehouse = _configured(wb, 'warehouse_keyword')
    mabang_user = _configured(mabang_login, 'username')
    mabang_password = _configured(mabang_login, 'password')
    mabang_refresh = bool(mabang_login.get('auto_refresh'))
    cookie_jar = mabang.get('cookie_jar')
    if isinstance(cookie_jar, list):
        mabang_session = any(isinstance(cookie, dict) and all(_configured(cookie, k) for k in ('name', 'value', 'domain'))
                             for cookie in cookie_jar)
    else:
        mabang_session = any(_configured(mabang, k) for k in ('www_cookie', 'aamz_cookie'))
    mabang_bearer = _configured(mabang, 'api_bearer')
    mabang_key = _configured(mabang, 'api_key')
    fulfillment = {'shop_map_configured': mapped_shops > 0, 'mapped_shops': mapped_shops,
                   'warehouse_configured': warehouse.mode not in ('missing', 'invalid'),
                   'warehouse_mode': warehouse.mode,
                   'handover_configured': any(_configured(mabang, k) for k in ('handover_keyword', 'handover_channel_value'))}
    platforms = {
        'bcs': {
            'configured': bcs_user or bcs_password or bcs_token or bcs_limit_key,
            'session_configured': bcs_token and bcs_limit_key,
            'login_configured': bcs_user,
            'password_configured': bcs_password,
            'auto_refresh': bcs_refresh,
            'auto_refresh_enabled': bcs_refresh and bcs_default and bcs_user and bcs_password,
            'endpoint': 'default' if bcs_default else 'custom',
        },
        'wb': {'configured': bool(ids) or wb_warehouse, 'root_version': root_version,
               'warehouse_keyword_configured': wb_warehouse},
        'mabang': {
            'configured': any((mabang_user, mabang_password, mabang_session, mabang_bearer, mabang_key,
                               fulfillment['shop_map_configured'], fulfillment['warehouse_configured'],
                               fulfillment['handover_configured'])),
            'login_configured': mabang_user,
            'session_configured': mabang_session,
            'api_configured': mabang_bearer and mabang_key,
            'warehouse_configured': fulfillment['warehouse_configured'],
            'warehouse_keyword_configured': bool(warehouse.keyword),
            'warehouse_id_configured': warehouse.warehouse_id is not None,
            'warehouse_mode': fulfillment['warehouse_mode'],
            'mapped_shops': mapped_shops,
            'login': {'username_configured': mabang_user, 'password_configured': mabang_password,
                      'auto_refresh': mabang_refresh,
                      'auto_refresh_enabled': mabang_refresh and mabang_user and mabang_password,
                      'session_configured': mabang_session},
            'fulfillment': fulfillment,
        },
        'feishu': {'configured': _configured(feishu, 'base_url')},
    }
    if parsed:
        status = 'ready' if any(platform['configured'] for platform in platforms.values()) else 'unconfigured'
        diagnosis.update(status='invalid_structure' if issues else status, json_valid=True, structure_valid=not issues)
    return {'credentials': diagnosis, 'platforms': platforms,
            'shop_counts': {'credential_configured': len(ids) if counts_known else None,
                            'credential_session_complete': len(complete) if counts_known else None}}


class Credentials:
    """凭证访问器：读 credentials.json，提供 BCS header 与 WB 店铺会话数据。"""

    def __init__(self, path=None):
        self.path = path or config.current.credentials_json
        self.reload()

    def reload(self):
        self.data = safe_load_json(self.path, default={})
        self.bcs = self.data.get("bcs") or {}
        self.wb = self.data.get("wb") or {}
        # 只保留三件套齐全的店铺
        self.shops = [s for s in self.wb.get("shops", [])
                      if s.get("authorizev3") and s.get("wb_seller_lk") and s.get("cookie")]
        self.root_version = self.wb.get("root_version", DEFAULT_ROOT_VERSION)
        self.ai = self.data.get("ai") or {}

    # ---- BCS 云端 API 凭证 ----
    @property
    def base_url(self):
        return self.bcs.get('base_url', DEFAULT_BCS_URL)

    @property
    def token(self):
        return self.bcs.get("token") or ""

    @property
    def limit_key(self):
        return self.bcs.get("limit_key") or ""

    @property
    def cookie_extra(self):
        return self.bcs.get("cookie_extra") or ""

    def bcs_headers(self):
        """BCS 请求头（Bearer + X-Limit-Key + Cookie）"""
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-HK;q=0.8,zh-TW;q=0.7",
            "Authorization": "Bearer " + self.token,
            "X-Limit-Key": self.limit_key,
            "Referer": "https://wb.bcserp.com/productList",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"),
            "Cookie": ("Admin-Token=" + self.token + "; Limit-Key=" + self.limit_key +
                       ";" + self.cookie_extra),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    # ---- AI 回复（OpenAI 兼容 LLM：DeepSeek / 商汤日日新 SenseNova / 通义 等） ----
    @property
    def ai_key(self):
        return self.ai.get("api_key") or self.ai.get("deepseek_key") or ""

    @property
    def ai_base_url(self):
        return self.ai.get("base_url") or "https://api.deepseek.com/chat/completions"

    @property
    def ai_model(self):
        return self.ai.get("model") or "deepseek-chat"

    @property
    def ai_watch_interval(self):
        try:
            return int(self.ai.get("watch_interval") or 90)
        except (TypeError, ValueError):
            return 90

    @property
    def ai_max_tokens(self):
        try:
            return int(self.ai.get("max_tokens") or 2000)
        except (TypeError, ValueError):
            return 2000

    # ---- WB 卖家后台 cookie 三件套 ----
    def feishu_base_url(self):
        return (self.data.get("feishu") or {}).get("base_url") or ""

    @property
    def wb_warehouse_keyword(self) -> str:
        keyword = self.wb.get("warehouse_keyword")
        return keyword.strip() if isinstance(keyword, str) else ""

    def wb_shops(self):
        return self.shops

    def wb_shop(self, shop_id):
        for s in self.shops:
            if s.get("shopId") == shop_id:
                return s
        return None

    def validate(self):
        """校验凭证完整性，返回 (ok, 问题列表)"""
        problems = []
        if not self.token:
            problems.append("bcs.token 为空（BCS 接口会 401）")
        if not self.limit_key:
            problems.append("bcs.limit_key 为空（网关会 405）")
        for s in self.wb.get("shops", []):
            if not (s.get("authorizev3") and s.get("wb_seller_lk") and s.get("cookie")):
                problems.append(f"WB 店铺 {s.get('shopName')}({s.get('shopId')}) 三件套不完整")
        return (len(problems) == 0), problems


@lru_cache(maxsize=1)
def _credentials(data: config.DataPaths) -> Credentials:
    return Credentials(data.credentials_json)


def get() -> Credentials:
    return _credentials(config.current)


def reload() -> Credentials:
    _credentials.cache_clear()
    return get()

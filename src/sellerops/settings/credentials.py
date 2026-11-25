"""
sellerops 统一凭证管理

单一凭证文件：data/credentials.json，结构：
{
  "bcs": { "base_url": "...", "token": "...", "limit_key": "...", "cookie_extra": "..." },
  "wb":  { "root_version": "v1.108.1", "shops": [ {shop_name, shop_id, authorizev3, wb_seller_lk, cookie}, ... ] }
}

BCS 三件套（token/limit_key/cookie_extra）+ WB 全部店铺 cookie 全在这里，换凭证只改一个文件。
"""


from functools import lru_cache
import json
import re

from sellerops.settings import paths as config
from sellerops.core.files import safe_load_json
from sellerops.domain.mabang_warehouse import warehouse_config
DEFAULT_ROOT_VERSION = "v1.108.1"
DEFAULT_BCS_URL = 'https://wb.bcserp.com/prod-api'


def _configured(section: dict, key: str) -> bool:
    value = section.get(key)
    return isinstance(value, str) and bool(value.strip())


def _invalid_json_constant(_value: str):
    raise ValueError('non-JSON numeric constant')


def inspect_credentials(path) -> dict:
    """Check the local structure and project configuration flags without copying credentials."""
    diagnosis = {'status': 'missing', 'json_valid': None, 'structure_valid': None, 'issues': []}
    issues = diagnosis['issues']
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
    ids, complete = set(), set()
    session_keys = ('authorizev3', 'wb_seller_lk', 'cookie')
    if not isinstance(rows, list):
        issues.append({'path': 'wb.shops', 'code': 'expected_array'})
    else:
        for index, row in enumerate(rows, start=1):
            location = f'wb.shops[{index}]'
            if not isinstance(row, dict):
                issues.append({'path': location, 'code': 'expected_object'})
                continue
            shop_id = row.get('shopId')
            if type(shop_id) is not int or shop_id <= 0:
                issues.append({'path': f'{location}.shopId', 'code': 'invalid_shop_id'})
                continue
            if shop_id in ids:
                issues.append({'path': f'{location}.shopId', 'code': 'duplicate_shop_id'})
            ids.add(shop_id)
            check_strings(row, location, ('shopName', *session_keys))
            if all(_configured(row, key) for key in session_keys):
                complete.add(shop_id)

    if parsed:
        diagnosis.update(status='invalid_structure' if issues else 'ready', json_valid=True,
                         structure_valid=not issues)
    counts_known = ((isinstance(raw, dict) and isinstance(data.get('wb', {}), dict) and isinstance(rows, list))
                    or diagnosis['status'] == 'missing')
    root = wb.get('root_version', DEFAULT_ROOT_VERSION if 'wb' in data else None)
    root_version = root if isinstance(root, str) and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', root) else None
    platforms = {
        'bcs': {'configured': bool(bcs), 'session_configured': all(_configured(bcs, k) for k in ('token', 'limit_key')),
                'login_configured': _configured(bcs_login, 'username'),
                'endpoint': 'default' if bcs.get('base_url', DEFAULT_BCS_URL) == DEFAULT_BCS_URL else 'custom'},
        'wb': {'configured': bool(wb), 'root_version': root_version,
               'warehouse_keyword_configured': _configured(wb, 'warehouse_keyword')},
        'mabang': {'configured': bool(mabang), 'login_configured': _configured(mabang_login, 'username'),
                   'session_configured': any(mabang.get(k) for k in ('www_cookie', 'aamz_cookie', 'cookies', 'cookie_jar')),
                   'api_configured': all(_configured(mabang, k) for k in ('api_bearer', 'api_key')),
                   'warehouse_configured': warehouse.mode not in ('missing', 'invalid'),
                   'warehouse_keyword_configured': bool(warehouse.keyword),
                   'warehouse_id_configured': warehouse.warehouse_id is not None,
                   'warehouse_mode': warehouse.mode,
                   'mapped_shops': len(shop_map)},
        'feishu': {'configured': _configured(feishu, 'base_url')},
    }
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

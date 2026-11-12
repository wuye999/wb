"""Refresh configured WB shops from one manually captured seller-portal request."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from sellerops.core.errors import DataConsistencyError, SellerOpsError, ValidationError
from sellerops.core.files import FileLock, atomic_dump_json, safe_load_json
from sellerops.core.utils import jwt_payload
from sellerops.integrations.wildberries import auth
from sellerops.settings import credentials, paths

_SUPPLIER_COOKIE = re.compile(r'(?:^|;)[ \t]*x-supplier-id-external[ \t]*(?:=[ \t]*([^;]*))?(?=;|$)')


def _payload(token: object) -> dict:
    if isinstance(token, str) and re.fullmatch(r'[\w-]+\.[\w-]+\.[\w-]+', token, re.ASCII):
        try:
            payload = jwt_payload(token)
        except ValueError:
            pass
        else:
            if isinstance(payload, dict):
                return payload
    return {}


def _user(token: object) -> str:
    user = _payload(token).get('user')
    return str(user) if type(user) in (str, int) else ''


def _supplier_id(value: object) -> str:
    if isinstance(value, str):
        try:
            supplier = str(UUID(value))
        except ValueError:
            pass
        else:
            if supplier == value.lower():
                return supplier
    return ''


def _token_supplier(token: object) -> str:
    data = _payload(token).get('data')
    return _supplier_id(data.get('Z-Sid')) if isinstance(data, dict) else ''


def _supplier_cookie(cookie: str) -> tuple[str, re.Match | None]:
    matches = list(_SUPPLIER_COOKIE.finditer(cookie))
    if not matches:
        return '', None
    if len(matches) != 1:
        raise ValidationError('Cookie 中的 x-supplier-id-external 重复')
    match = matches[0]
    supplier = _supplier_id((match[1] or '').strip())
    if not supplier:
        raise ValidationError('Cookie 中的 x-supplier-id-external 须为完整店铺 UUID')
    return supplier, match


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        key = key.lower()
        if key in result:
            raise ValidationError('抓包字段重复，无法唯一确认请求头')
        result[key] = value
    return result


def _capture_headers(text: str) -> tuple[str, str]:
    text = text.strip()
    fence = re.fullmatch(r'```[^\r\n]*\r?\n(.*?)\r?\n```', text, re.S)
    if fence:
        text = fence[1].strip()
    request = re.fullmatch(r'fetch\s*\((.*)\)\s*;?', text, re.S)
    if not request:
        raise ValidationError('请提供一个 Copy as fetch 请求，可使用 Markdown 代码围栏')
    try:
        url, options = json.loads('[' + request[1] + ']', object_pairs_hook=_unique_fields)
        if not isinstance(url, str) or not isinstance(options, dict):
            raise ValueError
        address = urlsplit(url)
    except (ValueError, RecursionError):
        raise ValidationError('请提供一个 Copy as fetch 请求，地址和参数须为 JSON 格式') from None
    if address.scheme != 'https' or not (address.hostname or '').endswith('.wildberries.ru'):
        raise ValidationError('请复制 WB 卖家后台的真实 HTTPS 请求')
    headers = options.get('headers')
    if not isinstance(headers, dict):
        raise ValidationError('抓包缺少 headers')
    authorize = headers.get('authorizev3')
    if not _user(authorize):
        raise ValidationError('抓包缺少完整的 authorizev3，或其 JWT payload 缺少 user')
    cookie = headers.get('cookie')
    if not isinstance(cookie, str) or not cookie.strip() or re.search(r'[^\x20-\x7e]', cookie):
        raise ValidationError('请从 Request Headers 补齐完整 Cookie')
    return authorize, cookie.strip()


def _wb_config(data: object) -> dict:
    wb = data.get('wb') if isinstance(data, dict) else None
    if not isinstance(wb, dict) or not isinstance(wb.get('shops'), list) or not wb['shops']:
        raise DataConsistencyError('请先在 credentials.json 的 wb.shops 配置店铺')
    return wb


def _target_suppliers(shops: list, user: str) -> dict[int, str]:
    targets = {}
    suppliers = set()
    for index, shop in enumerate(shops, start=1):
        shop_id = shop.get('shopId') if isinstance(shop, dict) else None
        if type(shop_id) is not int or shop_id <= 0 or shop_id in targets:
            raise DataConsistencyError(f'wb.shops 第 {index} 项的 shopId 无效或重复')
        stored_user = _user(shop.get('authorizev3'))
        if stored_user and stored_user != user:
            raise ValidationError(f'店铺 {shop_id} 与抓包的 WB 登录账号不一致')
        cookie = shop.get('cookie')
        if cookie is not None and not isinstance(cookie, str):
            raise ValidationError(f'店铺 {shop_id} 的 Cookie 格式无效')
        try:
            cookie_supplier, _ = _supplier_cookie(cookie or '')
        except ValidationError as exc:
            raise ValidationError(f'店铺 {shop_id}：{exc}') from None
        token_supplier = _token_supplier(shop.get('wb_seller_lk'))
        if cookie_supplier and token_supplier and cookie_supplier != token_supplier:
            raise ValidationError(f'店铺 {shop_id} 的 Cookie UUID 与已有 seller-lk 的 Z-Sid 冲突')
        supplier = cookie_supplier or token_supplier
        if not supplier or supplier in suppliers:
            raise ValidationError(f'店铺 {shop_id} 缺少完整店铺 UUID，或其 UUID 与其他店铺重复')
        targets[shop_id] = supplier
        suppliers.add(supplier)
    return targets


def refresh(capture: str, path: Path) -> list[int]:
    """Validate every candidate before atomically replacing the three session fields."""
    authorize, cookie = _capture_headers(capture)
    source, supplier_match = _supplier_cookie(cookie)
    if not supplier_match:
        raise ValidationError('抓包的 Cookie 缺少 x-supplier-id-external')
    path = path.resolve()
    wb = _wb_config(safe_load_json(path))
    shops = wb['shops']
    root_version = wb.get('root_version') or credentials.DEFAULT_ROOT_VERSION
    targets = _target_suppliers(shops, _user(authorize))
    if source not in targets.values():
        raise ValidationError('抓包的店铺 UUID 不在本地 wb.shops 中')

    start = supplier_match.start(1)
    end = start + len(supplier_match[1].rstrip())
    updates = {}
    for shop_id, supplier in targets.items():
        shop_cookie = cookie[:start] + supplier + cookie[end:]
        try:
            token = auth.sign_seller_token(authorize, shop_cookie)
            if _token_supplier(token) != supplier:
                raise ValidationError('签发的 seller-lk 的 Z-Sid 与目标店铺 UUID 不一致')
            session = {'authorizev3': authorize, 'wb_seller_lk': token, 'cookie': shop_cookie}
            auth.probe_session(session, root_version)
        except SellerOpsError as exc:
            raise ValidationError(f'店铺 {shop_id}：{exc}。凭证文件未更新') from None
        updates[shop_id] = session

    with FileLock(path):
        latest = safe_load_json(path)
        latest_shops = _wb_config(latest)['shops']
        if latest_shops != shops:
            raise DataConsistencyError('wb.shops 在签发期间已变更，凭证文件未更新，请重新运行')
        for shop in latest_shops:
            shop.update(updates[shop['shopId']])
        atomic_dump_json(path, latest, use_lock=False)
    return list(updates)


def run_cookies_update(args) -> int:
    path = Path(paths.HAR_DIR) / args.md_file
    try:
        capture = path.read_text(encoding='utf-8-sig')
    except UnicodeError:
        raise ValidationError('抓包文件须为 UTF-8 文本') from None
    updated = refresh(capture, Path(paths.CREDENTIALS_JSON))
    credentials.reload()
    print(f'credentials.json 已更新 {len(updated)} 家 WB 店铺，店铺 ID {", ".join(map(str, updated))}')
    return 0

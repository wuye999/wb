"""Validate WB account and shop identities and publish verified session updates."""
from pathlib import Path
import re
from uuid import UUID

from sellerops.core.errors import DataConsistencyError, SellerOpsError, ValidationError
from sellerops.core.files import FileLock, atomic_dump_json, safe_load_json
from sellerops.core.utils import jwt_payload
from sellerops.integrations.wildberries import auth
from sellerops.settings.credentials import DEFAULT_ROOT_VERSION

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


def account_id(token: object) -> str:
    user = _payload(token).get('user')
    return str(user) if type(user) in (str, int) else ''


def shop_uuid(value: object) -> str:
    if isinstance(value, str):
        try:
            supplier = str(UUID(value))
        except ValueError:
            pass
        else:
            if supplier == value.lower():
                return supplier
    return ''


def token_shop_uuid(token: object) -> str:
    data = _payload(token).get('data')
    return shop_uuid(data.get('Z-Sid')) if isinstance(data, dict) else ''


def shop_cookie(cookie: str) -> tuple[str, re.Match | None]:
    matches = list(_SUPPLIER_COOKIE.finditer(cookie))
    if not matches:
        return '', None
    if len(matches) != 1:
        raise ValidationError('Cookie 中的 x-supplier-id-external 重复')
    match = matches[0]
    supplier = shop_uuid((match[1] or '').strip())
    if not supplier:
        raise ValidationError('Cookie 中的 x-supplier-id-external 须为完整店铺 UUID')
    return supplier, match


def shop_bindings(shops: list, user: str) -> dict[int, str]:
    targets = {}
    suppliers = set()
    for index, shop in enumerate(shops, start=1):
        shop_id = shop.get('shopId') if isinstance(shop, dict) else None
        if type(shop_id) is not int or shop_id <= 0 or shop_id in targets:
            raise DataConsistencyError(f'wb.shops 第 {index} 项的 shopId 无效或重复')
        stored_user = account_id(shop.get('authorizev3'))
        if stored_user and stored_user != user:
            raise ValidationError(f'店铺 {shop_id} 与本次 WB 登录账号不一致')
        cookie = shop.get('cookie')
        if cookie is not None and not isinstance(cookie, str):
            raise ValidationError(f'店铺 {shop_id} 的 Cookie 格式无效')
        try:
            cookie_supplier, _ = shop_cookie(cookie or '')
        except ValidationError as exc:
            raise ValidationError(f'店铺 {shop_id}：{exc}') from None
        token_supplier = token_shop_uuid(shop.get('wb_seller_lk'))
        if cookie_supplier and token_supplier and cookie_supplier != token_supplier:
            raise ValidationError(f'店铺 {shop_id} 的 Cookie UUID 与已有 seller-lk 的 Z-Sid 冲突')
        supplier = cookie_supplier or token_supplier
        if not supplier or supplier in suppliers:
            raise ValidationError(f'店铺 {shop_id} 缺少完整店铺 UUID，或其 UUID 与其他店铺重复')
        targets[shop_id] = supplier
        suppliers.add(supplier)
    return targets


def update_shops(path: Path, expected: dict, shops: list, authorize: str, cookie: str) -> list[int]:
    """Sign and verify all selected shops before writing the WB section once."""
    if (not account_id(authorize) or not isinstance(cookie, str) or not cookie
            or re.search(r'[^\x20-\x7e]', cookie)):
        raise ValidationError('WB 登录会话缺少有效凭证')
    _, supplier_match = shop_cookie(cookie)
    if not supplier_match:
        raise ValidationError('WB 会话缺少当前店铺身份，请在卖家后台选择店铺后重试')
    root_version = expected.get('root_version') or DEFAULT_ROOT_VERSION
    targets = shop_bindings(shops, account_id(authorize))

    start = supplier_match.start(1)
    end = start + len(supplier_match[1].rstrip())
    updates = {}
    for shop_id, supplier in targets.items():
        scoped_cookie = cookie[:start] + supplier + cookie[end:]
        try:
            token = auth.sign_seller_token(authorize, scoped_cookie)
            if token_shop_uuid(token) != supplier:
                raise ValidationError('签发的 seller-lk 的 Z-Sid 与目标店铺 UUID 不一致')
            session = {'authorizev3': authorize, 'wb_seller_lk': token, 'cookie': scoped_cookie}
            auth.probe_session(session, root_version)
        except SellerOpsError as exc:
            raise ValidationError(f'店铺 {shop_id}：{exc}。凭证文件未更新') from None
        updates[shop_id] = session

    with FileLock(path):
        latest = safe_load_json(path, default={})
        if (latest.get('wb') or {}) != expected:
            raise DataConsistencyError('wb.shops 在签发期间已变更，凭证文件未更新，请重新运行')
        latest['wb'] = {**expected, 'shops': [{**shop, **updates[shop['shopId']]} for shop in shops]}
        atomic_dump_json(path, latest, use_lock=False)
    return list(updates)

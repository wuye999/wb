"""Read account and shop identifiers from WB session credentials."""
import re
from uuid import UUID

from sellerops.core.errors import ValidationError
from sellerops.core.utils import jwt_payload


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

"""解析商品报告通过标准输入接收的临时 BCS 会话。"""
import json
import re
from urllib.parse import urlsplit

from sellerops.integrations.bcs.session import BCSSession


MAX_CAPTURE_CHARS = 1024 * 1024


def _session(url: object, options: object) -> BCSSession:
    if not isinstance(url, str):
        raise ValueError('临时BCS抓包缺少有效请求地址')
    try:
        address = urlsplit(url)
        valid = (address.scheme == 'https' and address.hostname == 'wb.bcserp.com'
                 and address.port in {None, 443} and not address.username and not address.password
                 and address.path.startswith('/prod-api/'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('临时抓包仅接受现有BCS域名下的HTTPS接口')
    if not isinstance(options, dict) or not isinstance(options.get('headers'), dict):
        raise ValueError('临时BCS抓包缺少请求头对象')
    headers: dict[str, str] = {}
    for key, value in options['headers'].items():
        if not isinstance(key, str) or not isinstance(value, str) or key.lower() in headers:
            raise ValueError('临时BCS抓包请求头格式无效或重复')
        headers[key.lower()] = value
    bearer = re.fullmatch(r'Bearer\s+(\S+)', headers.get('authorization', '').strip(), re.IGNORECASE)
    if not bearer or not headers.get('x-limit-key', '').strip() or not headers.get('cookie', '').strip():
        raise ValueError('临时BCS抓包需要Authorization、X-Limit-Key和Cookie请求头')
    return BCSSession(bearer.group(1), headers['x-limit-key'].strip(), headers['cookie'].strip())


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('临时BCS抓包JSON字段重复，无法唯一确认认证信息')
        result[key] = value
    return result


def parse_sessions(text: str) -> list[BCSSession]:
    """接受一个或多个JSON形式fetch块及可选Markdown围栏；错误不包含输入原文。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('临时BCS抓包输入为空')
    if len(text) > MAX_CAPTURE_CHARS:
        raise ValueError('临时BCS抓包输入超过1MiB上限')
    text = re.sub(r'(?m)^[ \t]*```[^\r\n]*$', '', text.lstrip('\ufeff')).strip()
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    sessions: list[BCSSession] = []
    offset = 0

    def take(pattern: str) -> None:
        nonlocal offset
        match = re.compile(pattern).match(text, offset)
        if not match:
            raise ValueError('临时BCS抓包格式无效，请提供完整的JSON形式fetch请求')
        offset = match.end()

    try:
        while offset < len(text):
            take(r'\s*fetch\s*\(\s*')
            url, offset = decoder.raw_decode(text, offset)
            take(r'\s*,\s*')
            options, offset = decoder.raw_decode(text, offset)
            take(r'\s*\)\s*;?\s*')
            sessions.append(_session(url, options))
    except (json.JSONDecodeError, RecursionError):
        raise ValueError('临时BCS抓包格式无效，请提供完整的JSON形式fetch请求') from None
    if not sessions:
        raise ValueError('临时BCS抓包输入为空')
    return sessions

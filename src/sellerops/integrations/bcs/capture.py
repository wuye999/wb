"""只解析人工复制的 BCS fetch 文本；会话仅驻留内存，不执行或保存抓包。"""
from dataclasses import dataclass, field
import json
import re
from urllib.parse import urlsplit


BCS_BASE_URL = 'https://wb.bcserp.com/prod-api'
MAX_CAPTURE_CHARS = 1024 * 1024


@dataclass(frozen=True)
class BCSSession:
    """单次运行的 BCS 认证资料；所有秘密均不参与 repr。"""

    token: str = field(repr=False)
    limit_key: str = field(repr=False)
    cookie: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.token, self.limit_key, self.cookie):
            if not isinstance(value, str) or not value.strip() or re.search(r'[\x00-\x1f\x7f]', value):
                raise ValueError('临时BCS凭证缺少必要字段或包含无效字符')
        expected = {'Admin-Token': self.token, 'Limit-Key': self.limit_key}
        found: set[str] = set()
        for item in self.cookie.split(';'):
            key, sep, value = item.strip().partition('=')
            if sep and key in expected:
                if key in found or value != expected[key]:
                    raise ValueError('临时BCS请求头与Cookie的认证信息不一致')
                found.add(key)

    def headers(self) -> dict[str, str]:
        """每次返回独立请求头，不读取本机凭证。"""
        return {'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Authorization': 'Bearer ' + self.token,
                'X-Limit-Key': self.limit_key,
                'Cookie': self.cookie,
                'Referer': 'https://wb.bcserp.com/productList',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'}


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

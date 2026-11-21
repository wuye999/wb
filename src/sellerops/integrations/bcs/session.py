"""BCS 会话字段和请求头。"""
from dataclasses import dataclass, field
import re


BCS_BASE_URL = 'https://wb.bcserp.com/prod-api'
LOGIN_ORIGIN = 'https://bcserp.com'
LOGIN_URL = LOGIN_ORIGIN + '/prod-api/login'
SESSION_COOKIES = {'admin-token', 'limit-key', 'username', 'password', 'rememberme'}


@dataclass(frozen=True)
class BCSSession:
    """BCS 会话的认证资料，秘密字段不参与 repr。"""

    token: str = field(repr=False)
    limit_key: str = field(repr=False)
    cookie: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.token, self.limit_key, self.cookie):
            if not isinstance(value, str) or not value.strip() or re.search(r'[\x00-\x1f\x7f]', value):
                raise ValueError('BCS 凭证缺少必要字段或包含无效字符')
        expected = {'Admin-Token': self.token, 'Limit-Key': self.limit_key}
        found: set[str] = set()
        for item in self.cookie.split(';'):
            key, sep, value = item.strip().partition('=')
            if sep and key in expected:
                if key in found or value != expected[key]:
                    raise ValueError('BCS 请求头与 Cookie 的认证信息不一致')
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


def cookie_fields(header: str) -> dict[str, str]:
    """Parse Cookie without silently accepting ambiguous duplicate names."""
    if re.search(r'[\x00-\x1f\x7f]', header):
        raise ValueError('BCS Cookie 格式无效')
    cookies: dict[str, str] = {}
    for pair in header.split(';'):
        if not pair.strip():
            continue
        name, separator, value = pair.strip().partition('=')
        if not separator or not re.fullmatch(r"[\w!#$%&'*+.^`|~-]+", name, re.ASCII) or name in cookies:
            raise ValueError('BCS Cookie 格式无效或名称重复')
        cookies[name] = value
    return cookies


def extra_cookie_header(cookies: dict[str, str]) -> str:
    return '; '.join(f'{name}={value}' for name, value in cookies.items()
                     if name.lower() not in SESSION_COOKIES)


def login_session(token: object, limit_key: object, cookies: dict[str, str]) -> BCSSession:
    for value in (token, limit_key):
        if not isinstance(value, str) or not re.fullmatch(r'[^\s;]+', value):
            raise ValueError('BCS 登录响应缺少有效 token 或 limitKey')
    cookie = f'Admin-Token={token}; Limit-Key={limit_key}'
    extra = extra_cookie_header(cookies)
    return BCSSession(token, limit_key, f'{cookie}; {extra}' if extra else cookie)

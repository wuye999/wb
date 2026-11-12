"""Sign and check WB seller-portal sessions using a manually captured login."""
import requests

from sellerops.core.errors import AuthenticationError, NetworkError, PlatformApiError
from sellerops.core.utils import UA
from sellerops.integrations.wildberries import client

AUTH_URL = 'https://seller.wildberries.ru/ns/suppliers-auth/suppliers-portal-core/auth/token'


def _request(send, url: str, stage: str, **kwargs) -> requests.Response:
    try:
        response = send(url, timeout=20, allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise NetworkError(f'{stage}请求失败，请检查网络后重试') from None
    if response.status_code in (401, 403):
        raise AuthenticationError(f'{stage}返回 HTTP {response.status_code}，请核对登录会话和店铺')
    if response.status_code not in (200, 204):
        raise PlatformApiError(f'{stage}失败', status_code=response.status_code)
    return response


def _json_object(response: requests.Response, stage: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise PlatformApiError(f'{stage}未返回有效 JSON') from None
    if not isinstance(body, dict) or body.get('error'):
        raise PlatformApiError(f'{stage}返回错误')
    return body


def sign_seller_token(authorize: str, cookie: str) -> str:
    stage = 'WB 店铺令牌签发'
    response = _request(
        requests.post, AUTH_URL, stage,
        headers={'authorizev3': authorize, 'Cookie': cookie, 'User-Agent': UA},
        json={'params': {}, 'jsonrpc': '2.0', 'id': 'bridge'},
    )
    result = _json_object(response, stage).get('result')
    data = result.get('data') if isinstance(result, dict) else None
    token = data.get('token') if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise PlatformApiError(f'{stage}未返回 seller-lk')
    return token


def probe_session(shop: dict, root_version: str) -> None:
    stage = 'WB 店铺凭证探针'
    with client.make_session(shop, root_version) as session:
        response = _request(
            session.get, client.QUESTIONS_URL, stage,
            params={'limit': 5, 'cursor': '', 'isAnswered': 'false',
                    'searchText': '', 'sortOrder': 'dateDesc'},
        )
    if response.status_code == 204:
        return
    data = _json_object(response, stage).get('data')
    if not isinstance(data, dict) or 'questions' not in data:
        raise PlatformApiError(f'{stage}缺少提问列表')
    questions = data['questions']
    if questions is not None and not isinstance(questions, list):
        raise PlatformApiError(f'{stage}的提问列表格式无效')

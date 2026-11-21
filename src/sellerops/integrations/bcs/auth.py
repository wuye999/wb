"""BCS login, read-only account validation, and atomic session replacement."""
from pathlib import Path

import requests

from sellerops.core.errors import AuthenticationError, DataConsistencyError
from sellerops.core.files import FileLock, atomic_dump_json
from sellerops.integrations.bcs.session import (
    BCS_BASE_URL, LOGIN_ORIGIN, LOGIN_URL, BCSSession, cookie_fields, extra_cookie_header, login_session,
)
from sellerops.settings import credentials, paths
from sellerops.settings.credentials import Credentials


BUSINESS_HOME = 'https://wb.bcserp.com/productList'
LOGIN_PAGE = LOGIN_ORIGIN + '/login_v1'
PROFILE_URL = BCS_BASE_URL + '/system/user/profile'
TIMEOUT = (6, 10)
MANUAL_LOGIN = '请运行 login bcs，在 BCS 官网完成人工验证。'
_failed_refresh: dict[Path, tuple[Credentials, str]] = {}


class BrowserVerificationRequired(AuthenticationError):
    """The BCS login flow needs a human in the browser."""


def _login_settings(cred: Credentials) -> dict:
    login = cred.bcs.get('login')
    return login if isinstance(login, dict) else {}


def _has_field(login: dict, field: str) -> bool:
    value = login.get(field)
    return isinstance(value, str) and bool(value.strip())


def auto_refresh_enabled(cred: Credentials) -> bool:
    login = _login_settings(cred)
    return (cred.base_url == BCS_BASE_URL and login.get('auto_refresh', True) is True
            and _has_field(login, 'username') and _has_field(login, 'password'))


def _session_values(cred: Credentials) -> tuple[str, str, str]:
    return cred.token, cred.limit_key, cred.cookie_extra


def _same_config(left: Credentials, right: Credentials) -> bool:
    return left.base_url == right.base_url and _login_settings(left) == _login_settings(right)


def _same_generation(left: Credentials, right: Credentials) -> bool:
    return _same_config(left, right) and _session_values(left) == _session_values(right)


def _request(send, url: str, stage: str, **kwargs) -> requests.Response:
    try:
        response = send(url, timeout=TIMEOUT, allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise AuthenticationError(f'BCS {stage}请求失败，请检查网络。') from None
    if response.status_code in {403, 405, 412} or 300 <= response.status_code < 400:
        raise BrowserVerificationRequired(f'BCS {stage}遇到网关校验或访问限制。{MANUAL_LOGIN}')
    if response.status_code != 200:
        raise AuthenticationError(f'BCS {stage}失败（HTTP {response.status_code}）。')
    return response


def _json(response: requests.Response, stage: str) -> dict:
    try:
        data = response.json()
    except ValueError:
        raise BrowserVerificationRequired(f'BCS {stage}未返回登录数据，可能需要验证。{MANUAL_LOGIN}') from None
    if not isinstance(data, dict):
        raise AuthenticationError(f'BCS {stage}响应格式无效。')
    return data


def password_login(username: str, password: str) -> BCSSession:
    """Use ordinary server-issued cookies and submit the password only after an explicit no-slider response."""
    # Both hosts set a host-only cookie with the same name. Keep their jars separate.
    with requests.Session() as business, requests.Session() as login:
        _request(business.get, BUSINESS_HOME, '打开业务页')
        _request(login.get, LOGIN_PAGE, '打开登录页', params={'redirect': BUSINESS_HOME})
        headers = {'Accept': 'application/json, text/plain, */*', 'Referer': LOGIN_PAGE}
        check = _request(login.get, LOGIN_URL + '/slider-required', '检查滑块',
                         headers=headers, params={'username': username})
        precheck = _json(check, '检查滑块')
        if precheck.get('code') != 200 or precheck.get('needSlider') is not False:
            raise BrowserVerificationRequired(f'BCS 未确认可免滑块登录。{MANUAL_LOGIN}')
        response = _request(login.post, LOGIN_URL, '密码登录', headers={**headers, 'Origin': LOGIN_ORIGIN},
                            json={'username': username, 'password': password, 'captchaVerifyParam': ''})
        data = _json(response, '密码登录')
        if data.get('code') != 200:
            raise AuthenticationError(f'BCS 未接受密码登录，请核对本机账号密码及是否需要验证。{MANUAL_LOGIN}')
        try:
            request = business.prepare_request(requests.Request('GET', PROFILE_URL))
            cookies = cookie_fields(request.headers.get('Cookie', ''))
            return login_session(data.get('token'), data.get('limitKey'), cookies)
        except ValueError:
            raise AuthenticationError('BCS 未签发完整、有效的登录凭证。') from None


def verify_account(session: BCSSession, username: str) -> None:
    response = _request(requests.get, PROFILE_URL, '核对账号', headers=session.headers())
    result = _json(response, '核对账号')
    data = result.get('data')
    if result.get('code') != 200 or not isinstance(data, dict):
        raise AuthenticationError(f'BCS 新会话未通过账号鉴权。{MANUAL_LOGIN}')
    if data.get('userName') != username:
        raise AuthenticationError('BCS 会话归属与配置账号不一致，原凭证保留。')


def _save_session(path: Path, baseline: Credentials, session: BCSSession) -> Credentials:
    with FileLock(path):
        latest = Credentials(path)
        if not _same_generation(latest, baseline):
            raise AuthenticationError('BCS 配置在登录期间更新，已保留较新的本机凭证。')
        updates = {'token': session.token, 'limit_key': session.limit_key,
                   'cookie_extra': extra_cookie_header(cookie_fields(session.cookie))}
        atomic_dump_json(path, {**latest.data, 'bcs': {**latest.bcs, **updates}}, use_lock=False)
        return credentials.reload()


def connect_browser_session(baseline: Credentials, session: BCSSession) -> None:
    login = _login_settings(baseline)
    if baseline.base_url != BCS_BASE_URL or not _has_field(login, 'username'):
        raise AuthenticationError('请先核对 BCS 服务地址和本机配置的登录账号。')
    path = Path(baseline.path).resolve()
    with FileLock(path.with_name('bcs-auth'), timeout=90):
        verify_account(session, login['username'].strip())
        _save_session(path, baseline, session)
        _failed_refresh.pop(path, None)


def refresh_login(*, expected: Credentials | None = None) -> Credentials:
    """Serialize refreshes per data directory and reuse a concurrent successful renewal."""
    path = Path(paths.CREDENTIALS_JSON).resolve()
    with FileLock(path.with_name('bcs-auth'), timeout=90):
        baseline = credentials.reload()
        if not isinstance(baseline.bcs, dict):
            raise DataConsistencyError('credentials.json 的 bcs 必须是对象。')
        if baseline.base_url != BCS_BASE_URL:
            raise AuthenticationError('BCS 密码登录仅支持默认 BCS 服务地址。')
        if expected is not None:
            if not _same_config(baseline, expected):
                raise AuthenticationError('BCS 登录配置已更新，请重新运行命令。')
            if baseline.token and baseline.limit_key and _session_values(baseline) != _session_values(expected):
                return baseline
            failure = _failed_refresh.get(path)
            if failure is not None and _same_generation(baseline, failure[0]):
                raise AuthenticationError(failure[1])
        login = _login_settings(baseline)
        if not _has_field(login, 'username') or not _has_field(login, 'password'):
            raise AuthenticationError('请先在 credentials.json 配置 bcs.login 的账号和密码。')
        username = login['username'].strip()
        try:
            session = password_login(username, login['password'])
            verify_account(session, username)
            updated = _save_session(path, baseline, session)
        except AuthenticationError as error:
            if expected is not None:
                _failed_refresh[path] = (baseline, str(error))
            raise
        _failed_refresh.pop(path, None)
        return updated

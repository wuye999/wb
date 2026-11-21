"""Explicit, authentication-only WorkBuddy entry point."""
from pathlib import Path
import time

from sellerops.core.errors import AuthenticationError, ValidationError
from sellerops.core.files import FileLock, safe_load_json
from sellerops.integrations.bcs import auth as bcs
from sellerops.integrations.bcs import browser_session as bcs_browser
from sellerops.integrations.bcs.client import BCSClient
from sellerops.integrations.mabang import auth as mabang
from sellerops.integrations.mabang import browser_session as mabang_browser
from sellerops.integrations.mabang.session import MabangVerificationRequired
from sellerops.integrations.sessions import browser
from sellerops.integrations.wildberries import browser_session as wb_browser
from sellerops.integrations.wildberries import session_update
from sellerops.settings import credentials, paths


_ADAPTERS = {'bcs': bcs_browser, 'mabang': mabang_browser, 'wb': wb_browser}
_NAMES = {'bcs': 'BCS', 'mabang': '马帮', 'wb': 'WB'}
_HUMAN_LOGIN_SECONDS = {'bcs': 600, 'mabang': 600, 'wb': 1200}


def _probe_session(context, platform: str):
    if platform == 'wb':
        print('[正在连接] 正在打开 WB 后台并读取店铺列表，直连较慢时请等待。', flush=True)
    return _ADAPTERS[platform].read_session(context)


def _wait_until_logged_in(context, platform: str) -> None:
    from playwright.sync_api import Error

    adapter = _ADAPTERS[platform]
    end = time.monotonic() + _HUMAN_LOGIN_SECONDS[platform]
    while time.monotonic() < end and context.pages:
        for page in context.pages:
            try:
                if adapter.is_logged_in(page):
                    return
            except Error:
                if not page.is_closed():
                    raise
        if context.pages:
            try:
                context.pages[0].wait_for_timeout(500)
            except Error:
                if context.pages:
                    continue
    raise AuthenticationError('尚未完成网页登录。请重新运行登录命令，原凭证保留。')


def _browser_session(platform: str, *, resume: bool, keep_open: bool):
    adapter = _ADAPTERS[platform]
    profile = browser.profile_dir(platform)
    with FileLock(profile.with_suffix('.browser'), timeout=0):
        if profile.is_dir() and not keep_open:
            with browser.open_profile(platform) as context:
                restored = _probe_session(context, platform)
            if restored is not None:
                return restored
        if resume:
            raise AuthenticationError(f'专用浏览器暂无可用会话，请运行 login {platform} 登录。')

        with browser.open_profile(platform) as context:
            instruction = '登录后保持窗口打开，等待连接结果' if keep_open else '登录成功后正常关闭专用窗口'
            print(f'[等待人工登录] 请在{_NAMES[platform]}官网完成登录和验证。{instruction}。', flush=True)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(adapter.LOGIN_ENTRY, wait_until='commit', timeout=adapter.NAVIGATION_TIMEOUT_MS)
            if keep_open:
                _wait_until_logged_in(context, platform)
                restored = _probe_session(context, platform)
            else:
                browser.wait_for_window_close(context, timeout_ms=_HUMAN_LOGIN_SECONDS[platform] * 1000)
        if not keep_open:
            with browser.open_profile(platform) as context:
                restored = _probe_session(context, platform)
        if restored is None:
            guidance = ('请检查账号和平台登录提示后重试' if keep_open
                        else f'可运行 login {platform} --keep-open，保持窗口打开直到收到连接结果')
            raise AuthenticationError(f'浏览器未取得完整会话。{guidance}。原凭证保留。')
        return restored


def _bindings(raw: list[str]) -> dict[int, str]:
    bindings = {}
    used = set()
    for item in raw:
        local, _, supplier = item.partition('=')
        uuid = session_update.shop_uuid(supplier)
        if not local.isascii() or not local.isdecimal() or len(local) > 16 or int(local) <= 0 or not uuid:
            raise ValidationError('--bind 须使用已确认的 BCS 店铺 ID 和 WB 店铺 UUID')
        shop_id = int(local)
        if shop_id in bindings or uuid in used:
            raise ValidationError('新店绑定须一一对应，不能重复选择店铺')
        bindings[shop_id] = uuid
        used.add(uuid)
    return bindings


def _wb_targets(existing: list, bindings: dict[int, str], available: dict, username: str) -> list | None:
    targets = []
    remaining = dict(bindings)
    unbound = []
    for shop in existing:
        if not isinstance(shop, dict):
            raise ValidationError('本机 WB 店铺配置无效，原凭证保留。')
        shop_id = shop.get('shopId')
        if type(shop_id) is not int or shop_id <= 0:
            raise ValidationError('本机 WB 店铺 ID 无效，原凭证保留。')
        cookie = shop.get('cookie') or ''
        if not isinstance(cookie, str):
            raise ValidationError('本机 WB 店铺 Cookie 格式无效，原凭证保留。')
        # Keep legacy account/UUID evidence intact. The shared validator checks it again before signing.
        cookie_uuid, _ = session_update.shop_cookie(cookie)
        uuid = cookie_uuid or session_update.token_shop_uuid(shop.get('wb_seller_lk'))
        new_uuid = remaining.pop(shop_id, None)
        if uuid and new_uuid and uuid != new_uuid:
            raise ValidationError(f'店铺 {shop_id} 已绑定其他 WB 店铺，原凭证保留。')
        if not uuid and not new_uuid:
            unbound.append(shop_id)
            continue
        target = shop if uuid else {**shop, 'cookie': f'x-supplier-id-external={new_uuid}'}
        targets.append(target)
    if unbound:
        print(f'[待确认] 以下 BCS 店铺尚未绑定：{", ".join(map(str, unbound))}。')
        return None
    if bindings:
        bcs_shops = {shop.id: shop.name for shop in BCSClient().fetch_open_api_shops()}
        for shop_id in bindings:
            if shop_id not in bcs_shops:
                raise ValidationError(f'BCS 当前账号没有店铺 {shop_id}，原凭证保留。')
        for shop_id, uuid in remaining.items():
            targets.append({'shopId': shop_id, 'shopName': bcs_shops[shop_id],
                            'cookie': f'x-supplier-id-external={uuid}'})
    for shop_id, uuid in session_update.shop_bindings(targets, username).items():
        if uuid not in available:
            raise ValidationError(f'店铺 {shop_id} 不在当前 WB 账号的可用店铺中，原凭证保留。')
    return targets


def _connect_wb(path: Path, expected: dict, session: wb_browser.WBSession, bindings: dict[int, str]) -> int:
    available = {shop.uuid: shop for shop in session.shops}
    existing = expected.get('shops', [])
    if not isinstance(existing, list):
        raise ValidationError('本机 wb.shops 须为店铺列表，原凭证保留。')
    if not available:
        if existing or bindings:
            print('[已登录，暂无可用店铺] 本机已登记店铺的凭证未更新。')
            return 2
        print('[已登录，暂无店铺] WB 浏览器登录态已保留。')
        return 0
    if set(bindings.values()) - available.keys():
        raise ValidationError('选择的 WB 店铺不在当前账号的可用店铺列表中，原凭证保留。')
    targets = _wb_targets(existing, bindings, available, session_update.account_id(session.authorize))
    if not targets:
        print('[待确认店铺] 请向运营展示候选、确认 BCS 与 WB 的对应关系后，用 --bind 连接。')
        for shop in available.values():
            name = ''.join(ch for ch in shop.name if ch.isprintable())
            print(f'  {name} | WB 编号 {shop.old_id} | UUID {shop.uuid}')
        return 2
    updated = session_update.update_shops(path, expected, targets, session.authorize, session.cookie)
    credentials.reload()
    print(f'[已连接] WB 已更新 {len(updated)} 家店铺。店铺 ID {", ".join(map(str, updated))}。')
    return 0


def run(args) -> int:
    platform = args.platform
    if platform != 'wb' and args.bind:
        raise ValidationError('--bind 仅供 WB 确认新店对应关系')
    if platform == 'setup':
        if args.resume or args.keep_open:
            raise ValidationError('login setup 只安装和检查浏览器')
        browser.setup()
        return 0

    path = Path(paths.CREDENTIALS_JSON)
    bindings = _bindings(args.bind)
    baseline = credentials.reload()
    if platform == 'bcs':
        login = baseline.bcs.get('login') or {}
        if not login.get('username'):
            raise ValidationError('请先在本机 credentials.json 填写 BCS 登录账号。')
        if not (args.resume or args.keep_open) and login.get('password'):
            try:
                bcs.refresh_login()
            except bcs.BrowserVerificationRequired:
                pass
            else:
                print('[已连接] BCS。')
                return 0
    elif platform == 'mabang':
        cred = baseline.data.get('mabang') or {}
        login = cred.get('login') or {}
        if not login.get('username'):
            raise ValidationError('请先在本机 credentials.json 填写马帮登录账号。')
        if not (args.resume or args.keep_open) and login.get('password'):
            try:
                mabang.refresh_login(cred)
            except MabangVerificationRequired:
                pass
            else:
                print('[已连接] 马帮。')
                return 0

    session = _browser_session(platform, resume=args.resume, keep_open=args.keep_open)
    if platform == 'bcs':
        bcs.connect_browser_session(baseline, session)
    elif platform == 'mabang':
        mabang.connect_browser_session(cred, session.username, session.cookies)
    else:
        return _connect_wb(path, baseline.wb, session, bindings)
    print(f'[已连接] {_NAMES[platform]}。')
    return 0

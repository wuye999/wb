"""Explicit, authentication-only WorkBuddy entry point."""
from collections import Counter
from pathlib import Path
import time

from sellerops.core.errors import AuthenticationError, NetworkError, PlatformApiError, RateLimitError, ValidationError
from sellerops.core.files import FileLock, safe_load_json
from sellerops.integrations.bcs import auth as bcs
from sellerops.integrations.bcs import browser_session as bcs_browser
from sellerops.integrations.bcs.client import BCSClient
from sellerops.integrations.bcs.session import BCS_BASE_URL, BCSSession
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


def _display_name(name: str) -> str:
    return ''.join(ch for ch in name if ch.isprintable()).replace('|', '｜')


def _preview_wb_bindings(existing: list, available: dict, cred: credentials.Credentials) -> None:
    print('[待确认店铺] 请核对 BCS 与 WB 对应关系，再用 --bind 连接。')
    print('  WB 店铺 | WB 编号 | UUID')
    for shop in available.values():
        print(f'  {_display_name(shop.name)} | {shop.old_id} | {shop.uuid}')

    if not (cred.token and cred.limit_key):
        print('[配对提示] BCS 暂无已保存会话，请运行 login bcs 后重新查看。')
        return
    if cred.base_url != BCS_BASE_URL:
        print('[配对提示] 当前 BCS 地址不支持自动读取，请人工核对店铺。')
        return
    try:
        saved = BCSSession(cred.token, cred.limit_key, cred.bcs_headers()['Cookie'])
        candidates = BCSClient(saved).fetch_binding_shops()
    except (AuthenticationError, TypeError, ValueError):
        print('[配对提示] BCS 会话不可用，请运行 login bcs 后重新查看。')
        return
    except (NetworkError, PlatformApiError, RateLimitError):
        print('[配对提示] 暂时无法取得完整 BCS 店铺清单，请人工核对或稍后重试。')
        return
    if not candidates:
        print('[配对提示] BCS 当前账号未返回店铺，请核对登录账号。')
        return

    sids = [session_update.shop_uuid((shop['sid'] or '').strip()) for shop in candidates]
    sid_counts = Counter(sid for sid in sids if sid)
    local_counts = Counter(shop['shopId'] for shop in existing)
    local_bound = set()
    local_uuids = set()
    for shop in existing:
        cookie_uuid, _ = session_update.shop_cookie(shop.get('cookie') or '')
        token_uuid = session_update.token_shop_uuid(shop.get('wb_seller_lk'))
        if cookie_uuid or token_uuid:
            local_bound.add(shop['shopId'])
            local_uuids.update(uuid for uuid in (cookie_uuid, token_uuid) if uuid)

    recommendations = {}
    print('  BCS 店铺 ID | BCS 店铺 | WB 配对或原因')
    for shop, sid in zip(candidates, sids):
        shop_id = shop['id']
        if not sid:
            reason = '缺少有效 sid'
        elif sid_counts[sid] != 1:
            reason = 'sid 对应多个 BCS 店铺'
        elif sid not in available:
            reason = '当前 WB 账号无对应 UUID'
        elif local_counts[shop_id] > 1 or shop_id in local_bound or sid in local_uuids:
            reason = '涉及本机已有绑定或配置冲突'
        elif len(str(shop_id)) > 16:
            reason = 'BCS 店铺 ID 超出 --bind 支持范围'
        else:
            wb_shop = available[sid]
            reason = f'推荐 {_display_name(wb_shop.name)} / {wb_shop.old_id} / {sid}'
            recommendations[shop_id] = sid
        print(f'  {shop_id} | {_display_name(shop["name"])} | {reason}')

    if recommendations:
        missing = sorted(local_counts.keys() - local_bound - recommendations.keys())
        if missing:
            print(f'[部分推荐] 仍需确认本机待绑定店铺 {", ".join(map(str, missing))}，请补齐参数后运行。')
        elif len(recommendations) != len(candidates) or len(recommendations) != len(available):
            print('[部分推荐] 请根据上表人工核对其余店铺。')
        command = ' '.join(f'--bind {shop_id}={sid}' for shop_id, sid in recommendations.items())
        print(f'  login wb --resume {command}')
    else:
        print('[配对提示] 暂无可唯一确认的推荐，请根据候选人工核对。')


def _connect_wb(path: Path, expected: dict, session: wb_browser.WBSession,
                bindings: dict[int, str], cred: credentials.Credentials) -> int:
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
        _preview_wb_bindings(existing, available, cred)
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

    path = Path(paths.current.credentials_json)
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
        return _connect_wb(path, baseline.wb, session, bindings, baseline)
    print(f'[已连接] {_NAMES[platform]}。')
    return 0

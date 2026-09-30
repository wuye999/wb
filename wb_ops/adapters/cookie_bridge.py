# -*- coding: utf-8 -*-
"""
WB 会话 cookie 桥（Playwright 无头浏览器）

背景（2026-09-28 排查结论）：
    WB 卖家会话存在「真实浏览器指纹信标」门槛 —— /sec/api/fl 信标由混淆 JS 生成加密
    payload，纯 Python 重放无法注册（重放 200 但签发的新 cfidsw-wb 依旧 401）。
    浏览器活跃后 ~10-15 分钟窗口内，任何静态凭证（含过期 seller-lk）都能通过；
    窗口外三域（dp-api / seller-content / marketplace）一体 401。

本桥原理：
    Playwright 无头持久化 context = 真实浏览器环境，信标 JS 自然运行使会话自持。
    播种 credentials.json 既有登录态 → 打开卖家后台等待 validate 通过 →
    收割全量 cookie（含 HttpOnly）回写 credentials.json → wb_client 401 钩子自动重试。

用法：
    from wb_ops.adapters import cookie_bridge
    cookie_bridge.refresh_shops([9352], apply=True)          # 指定店刷新
    cookie_bridge.try_refresh(shop_id)                        # 401 钩子用（带冷却）
"""
import time
from typing import Any, Optional

from wb_ops import config, credentials, common
from wb_ops.framework.safe_io import atomic_dump_json, safe_load_json

SELLER_HOME = "https://seller.wildberries.ru/"
PROFILE_ROOT = config.DATA_DIR + "/wb_browser_profiles"
LOGIN_WAIT_S = 240          # 有头模式等待人工登录的最长秒数
SETTLE_S = 8                # 页面加载后静置秒数（让信标发出）
COOLDOWN_S = 120            # 401 钩子冷却：同店两次桥刷新最小间隔


def _chrome_executable() -> Optional[str]:
    """chromium_headless_shell 缺失时，回退用已安装的完整版 chrome（无头模式同样可用）。"""
    import os
    local = os.environ.get("LOCALAPPDATA", "")
    for rel in ("chromium-1243/chrome-win64/chrome.exe",):
        p = os.path.join(local, "ms-playwright", rel)
        if os.path.exists(p):
            return p
    return None

_LAST_REFRESH: dict[int, float] = {}    # shop_id -> 上次桥刷新时间戳
_PLAYWRIGHT_UNAVAILABLE = False


def profile_dir(shop_id: int) -> str:
    """店铺专用浏览器 profile 目录（持久化登录态）。"""
    return f"{PROFILE_ROOT}/{shop_id}"


def _cookie_seed(shop: dict[str, Any]) -> list[dict[str, str]]:
    """把 credentials 的 cookie 串转成 Playwright cookie 列表（domain=.wildberries.ru）。"""
    seeds = []
    for pair in (shop.get("cookie") or "").split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            seeds.append({"name": name.strip(), "value": value.strip(),
                          "domain": ".wildberries.ru", "path": "/"})
    return seeds


def _is_login_page(url: str) -> bool:
    """判断当前 URL 是否为登录/SSO 页。"""
    low = (url or "").lower()
    return any(k in low for k in ("login", "sso", "passport", "id.wildberries", "ozon"))


def _harvest_seller_lk(page) -> Optional[str]:
    """页面内调 auth/token 签发最新 seller-lk（带浏览器 cookie，天然过门槛）。

    同时作为登录态判定：返回 None 即会话未就绪（未登录/SSO 失效）。
    """
    try:
        return page.evaluate(
            """async () => {
                try {
                    const r = await fetch(
                        '/ns/suppliers-auth/suppliers-portal-core/auth/token',
                        {method: 'POST', credentials: 'include',
                         headers: {'content-type': 'application/json'},
                         body: JSON.stringify({params: {}, jsonrpc: '2.0', id: 'bridge'})});
                    if (!r.ok) return null;
                    const j = await r.json();
                    return (j.result && j.result.data && j.result.data.token) || null;
                } catch (e) { return null; }
            }"""
        )
    except Exception:
        return None


def refresh_shop(shop_id: int, headed: bool = False, login_wait_s: int = LOGIN_WAIT_S) -> dict[str, Any]:
    """无头浏览器刷新单店会话并回写 credentials.json。

    Args:
        shop_id: WB 店铺 ID（credentials.json wb.shops）。
        headed: True 时弹出有头窗口（首次播种登录态被拒时人工登录用）。
        login_wait_s: 有头模式等待人工登录的最长秒数。

    Returns:
        {"ok": bool, "shopName": str, "message": str}
    """
    global _PLAYWRIGHT_UNAVAILABLE
    cred = credentials.get()
    shop = cred.wb_shop(shop_id)
    if not shop:
        return {"ok": False, "shopName": str(shop_id), "message": f"credentials.json 中无店铺 {shop_id}"}
    name = f"{shop.get('shopName')}({shop_id})"

    if _PLAYWRIGHT_UNAVAILABLE:
        return {"ok": False, "shopName": name, "message": "playwright 不可用（未安装/安装失败）"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _PLAYWRIGHT_UNAVAILABLE = True
        return {"ok": False, "shopName": name, "message": "playwright 未安装（pip install playwright && playwright install chromium）"}

    print(f"[cookie桥] {name}: 启动无头浏览器（{'有头' if headed else '无头'}）...")
    try:
        with sync_playwright() as p:
            launch_kw: dict[str, Any] = {"headless": not headed,
                                         "viewport": {"width": 1380, "height": 900}}
            exe = _chrome_executable()
            if exe:
                launch_kw["executable_path"] = exe
            context = p.chromium.launch_persistent_context(
                profile_dir(shop_id), **launch_kw,
            )
            try:
                if _cookie_seed(shop):
                    context.add_cookies(_cookie_seed(shop))
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(SELLER_HOME, wait_until="domcontentloaded", timeout=60_000)

                # 等待真实登录态：页内 auth/token 能签出 token（SSO 会话有效）才算就绪
                deadline = time.time() + (login_wait_s if headed else 40)
                new_lk: Optional[str] = None
                while time.time() < deadline:
                    new_lk = _harvest_seller_lk(page)
                    if new_lk:
                        break
                    if headed:
                        print(f"[cookie桥] {name}: 请在弹出的浏览器中登录卖家后台...（剩余 {int(deadline - time.time())}s）")
                    time.sleep(4)
                if not new_lk:
                    return {"ok": False, "shopName": name,
                            "message": "会话未就绪（auth/token 签发失败）：播种登录态无效或人工登录超时；"
                                       "请用 --headed 重跑并登录一次"}

                time.sleep(SETTLE_S)  # 静置让 /sec/api/fl 信标发出
                cookies = [c for c in context.cookies()
                           if "wildberries.ru" in (c.get("domain") or "")]
                if not any(c["name"] == "cfidsw-wb" for c in cookies):
                    return {"ok": False, "shopName": name, "message": "收割失败：浏览器 cookie 中无 cfidsw-wb"}

                new_cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

                _write_back(shop_id, new_cookie, new_lk)
                print(f"[cookie桥] {name}: ✓ 收割 {len(cookies)} 条 cookie 已回写 credentials.json")
                return {"ok": True, "shopName": name, "message": "刷新成功"}
            finally:
                context.close()
    except Exception as e:  # noqa: BLE001 —— 桥失败必须降级为可读消息，不允许中断调用方业务
        return {"ok": False, "shopName": name, "message": f"浏览器刷新异常: {e}"}


def _write_back(shop_id: int, cookie: str, seller_lk: str) -> None:
    """收割结果原子回写 credentials.json 并重载单例。"""
    cfg_path = config.CREDENTIALS_JSON
    cfg = safe_load_json(cfg_path, default={})
    for s in (cfg.get("wb") or {}).get("shops") or []:
        if s.get("shopId") == shop_id:
            s["cookie"] = cookie
            if seller_lk:
                s["wb_seller_lk"] = seller_lk
            s["_comment"] = f"cookie桥更新 {time.strftime('%Y-%m-%d %H:%M')}"
            break
    atomic_dump_json(cfg_path, cfg, indent=2, use_lock=True)
    credentials.reload()


def try_refresh(shop_id: int) -> bool:
    """401 钩子入口：带冷却的单店自动刷新（同店 COOLDOWN_S 内只刷一次）。"""
    now = time.time()
    last = _LAST_REFRESH.get(shop_id, 0)
    if now - last < COOLDOWN_S:
        print(f"[cookie桥] 店 {shop_id}: 冷却中（距上次刷新 {int(now - last)}s），跳过")
        return False
    _LAST_REFRESH[shop_id] = now
    res = refresh_shop(shop_id)
    if not res["ok"]:
        print(f"[cookie桥] 店 {shop_id}: ✗ {res['message']}")
    return bool(res["ok"])


def refresh_shops(shop_ids: Optional[list[int]] = None, headed: bool = False,
                  apply: bool = False) -> dict[int, dict[str, Any]]:
    """CLI 入口：批量刷新（默认 dry-run 只预览，--apply 实际执行）。

    Returns:
        {shop_id: {"ok": bool, "shopName": str, "message": str}}
    """
    cred = credentials.get()
    shops = cred.wb_shops()
    if shop_ids:
        shops = [s for s in shops if s.get("shopId") in shop_ids]
    results: dict[int, dict[str, Any]] = {}
    if not apply:
        for s in shops:
            print(f"  [dry-run] {s.get('shopName')}({s.get('shopId')}) 将启动无头浏览器刷新会话")
            results[s.get("shopId")] = {"ok": None, "shopName": s.get("shopName"),
                                        "message": "dry-run 未执行"}
        return results
    for s in shops:
        results[s.get("shopId")] = refresh_shop(s.get("shopId"), headed=headed)
    return results


def run_cookie_refresh_wb(args) -> int:
    """CLI cookie-refresh-wb 入口：无头浏览器刷新 WB 会话。"""
    common.ensure_utf8_stdout()
    shop_ids = None
    raw = (getattr(args, "shops", "") or "").strip()
    if raw:
        shop_ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    results = refresh_shops(shop_ids, headed=getattr(args, "headed", False),
                            apply=getattr(args, "apply", False))
    ok = sum(1 for r in results.values() if r["ok"] is True)
    print(f"[汇总] 成功 {ok}/{len(results)}")
    return 0 if ok == len(results) and results else (0 if not getattr(args, "apply", False) else 1)

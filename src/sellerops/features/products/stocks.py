"""预览和提交本店莫斯科仓的全部目标规格。实时读取仅用于补充当前库存。"""
import time
from datetime import datetime
from sellerops.features.catalog.selection import load_state, selected_shops, resolve_filters
from sellerops.features.products.plans import RED, GREEN, RESET, plan_stock, dry_run

from sellerops.integrations.wildberries import client as wb_api
from sellerops.integrations.wildberries import stocks as wsc
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.features.products import operations as ops
from sellerops.features.products.product_data import ProductDataResolver
from sellerops.domain.snapshot_values import positive_id, quantity

from sellerops.features.products.skip_report import SkipTracker
from sellerops.persistence.product_results import write_product_receipts

# portal stocks POST 数组单批条数：官方同构端点上限 1000，_scratch 探针实测 1000 通过，留边际取 500
STOCK_CHUNK = 500
BATCH_SLEEP = 0.3
SHOP_SLEEP = 0.6
ACTION_TAG = "stock_wb"   # ops_result.csv 操作列标签（与 BCS 通道的 "stock" 区分，便于追溯）


# ---------- 目标圈定 ----------

# ---------- 计划构建 ----------

def _live_stock_map(session, max_pages, warehouse_id):
    """只读已确认莫斯科仓；库存缺行只表示未知，不决定完整规格集合。"""
    stocks = wsc.get_stocks(session, store_id=warehouse_id, verbose=True, max_pages=max_pages)
    mapping = {}
    for s in stocks:
        art = s.get("article")
        chrt = positive_id(s.get("chrtId"))
        if not art or chrt is None or (s.get('storeId') is not None and positive_id(s['storeId']) != warehouse_id):
            raise ValueError('实时库存标识无效或含非莫斯科仓记录')
        key = (str(art), chrt)
        if key in mapping:
            raise ValueError('实时库存存在重复规格记录')
        mapping[key] = quantity(s.get('amount'))
    return mapping


def build_plans(vcs, shops, state, amount, resolve="snapshot", max_pages=200, *, shop_rows=None):
    """逐店组装改库存计划。resolve 见模块 docstring。产物结构与 ops_plan.plan_stock 一致。"""
    if resolve not in {'snapshot', 'live'} or type(max_pages) is not int or max_pages < 1:
        raise ValueError('库存解析模式或分页上限无效')
    with ProductDataResolver() as resolver:
        plans = plan_stock(vcs, shops, state, amount, resolver=resolver, shop_rows=shop_rows)
    if resolve == 'snapshot':
        return plans

    # live仅补充当前库存；不能用库存缺行排除已确认的商品规格。
    cred = credentials.get()
    tracker = SkipTracker()
    for plan in plans:
        sid = plan['shopId']
        if not plan['items']:
            continue
        shop = cred.wb_shop(sid)
        session = None
        try:
            if not shop:
                raise ValueError('缺少WB网页凭证')
            wh = plan['warehouses'][0]['warehouseId']
            session = wb_api.make_session(shop, cred.root_version)
            mapping = _live_stock_map(session, max_pages, wh)
        except Exception as error:
            reason = ('缺少WB网页凭证' if not shop else f'莫斯科实时库存读取失败（{type(error).__name__}）')
            targets = {item['vc']: item for item in plan['items']}
            for vc, item in targets.items():
                plan.setdefault('blocked_items', []).append({'vc': vc, 'cn': item.get('cn', ''), 'reason': reason})
                tracker.add(sid, vc, reason)
            plan['items'], plan['warehouses'] = [], []
        else:
            for item in plan['items']:
                current = mapping.get((item['vc'], item['chrtId']))
                item.update(cur_amt=current, orig_zero=current == 0, wh_fallback=current is None)
        finally:
            if session is not None:
                session.close()
    if tracker.count:
        tracker.report(f"设库存(WB实时) {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


# ---------- 执行 ----------

def exec_online(session, plan, chunk, interval, results, ts):
    """单店提交：逐仓库分块 POST portal stocks。返回 (ok, fail)。"""
    sid = plan["shopId"]
    ok = fail = 0
    for wh in plan["warehouses"]:
        sis = wh["stockItems"]
        for i in range(0, len(sis), chunk):
            part = sis[i:i + chunk]
            try:
                r = wsc.post_stocks(session, wh["warehouseId"], part)
                ok_flag = r.get("error") is False
                msg = ("OK" if ok_flag else str(r.get("errorText") or "平台拒绝") if r.get("error") is True
                       else "结果未知：回执缺少明确状态")
            except common.CookieExpiredError as error:
                pending = sis[i:]
                results.extend([sid, it["chrtId"], ACTION_TAG, it["amount"], f"鉴权中止：{error}", ts]
                               for it in pending)
                return ok, fail + len(pending)
            except Exception as e:
                ok_flag, msg = False, f"FAIL: {e}"
            label = f"stock_wb 仓库{wh['warehouseId']} 批{i // chunk + 1}（{len(part)} 条）"
            if ok_flag:
                ok += len(part)
                print(f"  {GREEN}✓{RESET} {label} 成功")
            else:
                fail += len(part)
                print(f"  {RED}✗{RESET} {label} 失败: {msg}")
            for it in part:
                results.append([sid, it["chrtId"], ACTION_TAG, it["amount"], msg if ok_flag else f"FAIL: {msg}", ts])
            if i + chunk < len(sis):
                time.sleep(interval)
    return ok, fail


def _report_zero_items(plans):
    zero = [(p["shopId"], it) for p in plans for it in p["items"] if it.get("orig_zero")]
    if not zero:
        return
    print(f"\n{RED}⚠ [0 值商品报告] {len(zero)} 项操作前库存为 0（WB 官方数据延迟或商品受限）{RESET}")
    print("  已照常提交修改；若之后查询仍为 0，属 WB 官方原因（修改生效可能延迟），无需反复修改。")
    seen = {}
    for sid, it in zero:
        d = seen.setdefault(it["vc"], {"cn": it.get("cn", ""), "shops": [],
                                       "old": it.get("cur_amt"), "new": it["amount"]})
        d["shops"].append(sid)
    for vc, d in sorted(seen.items()):
        print(f"    {vc} | {d['cn']} | 原值 {d['old']} → 目标 {d['new']} | 店 {','.join(map(str, d['shops']))}")


# ---------- CLI 主入口 ----------

def run(args):
    common.validate_batch_options(chunk=getattr(args, "chunk", None),
                                  interval=getattr(args, "interval", None))
    if getattr(args, "amount", 0) < 0:
        print("[错误] --amount 不能为负数")
        return 1

    shops = selected_shops(args)
    state, shop_rows, boss = load_state(shops, all_products=getattr(args, "all_products", False))
    vcs, desc = resolve_filters(args, state, boss)
    if not vcs:
        print("[提示] 筛选结果为空，无操作")
        return 0
    print(f"目标: {desc}（{len(vcs)} 个 vendorCode）· 通道=WB 原生在线接口 · 解析={getattr(args, 'resolve', 'snapshot')}")
    plans = build_plans(vcs, shops, state, getattr(args, "amount", 0),
                        resolve=getattr(args, "resolve", "snapshot"),
                        max_pages=getattr(args, "max_pages", 200), shop_rows=shop_rows)
    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法操作（上方 [跳过] / [跳过汇总] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / sizeList 无 chrtId / 无 WB 凭证。")
        return 0

    for plan in plans:
        print(f"店 {plan['shopId']}：可执行商品 {len({it['vc'] for it in plan['items']})} 个，"
              f"规格 {len(plan['items'])} 个，阻止 {len(plan.get('blocked_items', []))} 项")
    if not getattr(args, "apply", False):
        dry_run(plans, "stock", getattr(args, "amount", None))
        return 1 if any(p.get("blocked_items") for p in plans) else 0

    if not ops.confirm_irreversible("stock", getattr(args, "amount", 0), getattr(args, "yes", False)):
        print("已取消，未执行任何操作")
        return 0

    cred = credentials.get()
    chunk = min(int(getattr(args, "chunk", STOCK_CHUNK)), wsc.MAX_CHUNK)
    interval = float(getattr(args, "interval", BATCH_SLEEP))
    ok = fail = 0
    results = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pi, p in enumerate(plans):
        sid = p["shopId"]
        for it in p.get("blocked_items", []):
            fail += 1
            print(f"  [已阻止] 店{sid} {it['vc']}: {it['reason']}")
            results.append([sid, it['vc'], ACTION_TAG, getattr(args, "amount", 0),
                            f"SKIP: {it['reason']}", ts])
        if not p["items"]:
            continue
        print(f"\n>>> 店{sid}：stock_wb（{len(p['items'])} 条）")
        shop = cred.wb_shop(sid)
        if not shop:
            print(f"  {RED}✗{RESET} 无 WB 凭证，跳过（{len(p['items'])} 条计入失败）")
            fail += len(p["items"])
            results.extend([sid, it["chrtId"], ACTION_TAG, it["amount"], "SKIP: 无 WB 凭证", ts]
                           for it in p["items"])
            continue
        try:
            session = wb_api.make_session(shop, cred.root_version)
            o, f = exec_online(session, p, chunk, interval, results, ts)
            ok += o
            fail += f
        except common.CookieExpiredError as e:
            print(f"  {RED}✗{RESET} cookie 失效，本店中止: {e}")
            fail += len(p["items"])
            results.extend([sid, it["chrtId"], ACTION_TAG, it["amount"], "SKIP: cookie 失效", ts]
                           for it in p["items"])
        if pi < len(plans) - 1:
            time.sleep(SHOP_SLEEP)

    write_product_receipts(config.current, plans, results)
    print(f"\n结果: 已受理 {ok} · 失败或未确认 {fail}（明细已保存 {config.current.result_csv}）")
    _report_zero_items(plans)

    common.print_write_hint()
    return 1 if fail else 0


def set_stock(shop, warehouse_id, chrt_items, *, root_version=None,
              chunk=STOCK_CHUNK, interval=BATCH_SLEEP) -> dict:
    """单店批量设库存（WB 原生 portal 接口，最底层门面）。

    Args:
        shop: credentials wb_shop(sid) 店铺 dict（含 authorizev3/wb_seller_lk/cookie）
        warehouse_id: WB 仓库 id（与 BCS wbWarehouses 同体系）
        chrt_items: [{"chrtId": X, "amount": N}, ...]（内部自动分块）
        chunk: 单批条数（自动夹取 ≤wsc.MAX_CHUNK=1000）
        interval: 批间间隔秒

    Returns:
        {"ok": int, "fail": int, "details": [{"chrtId","amount","ok","msg"}, ...]}
    异常：cookie 失效抛 common.CookieExpiredError，由调用方处理。
    """
    common.validate_batch_options(chunk=chunk, interval=interval)
    session = wb_api.make_session(shop, root_version)
    chunk = max(1, min(int(chunk), wsc.MAX_CHUNK))
    ok = fail = 0
    details = []
    for i in range(0, len(chrt_items), chunk):
        part = chrt_items[i:i + chunk]
        try:
            r = wsc.post_stocks(session, warehouse_id, part)
            ok_flag = r.get("error") is False
            msg = "OK" if ok_flag else (r.get("errorText") or "error=true")
        except common.CookieExpiredError:
            raise
        except Exception as e:
            ok_flag, msg = False, f"FAIL: {e}"
        for it in part:
            details.append({"chrtId": it["chrtId"], "amount": it["amount"],
                            "ok": ok_flag, "msg": msg})
        ok, fail = ok + (len(part) if ok_flag else 0), fail + (0 if ok_flag else len(part))
        if i + chunk < len(chrt_items):
            time.sleep(interval)
    return {"ok": ok, "fail": fail, "details": details}



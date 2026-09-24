# -*- coding: utf-8 -*-
"""
wb_ops WB 原生在线接口批量改库存 (stock_wb)

接口（抓包来源：api/网络请求/wb在线加载修改库存.har、wb在线根据供应商代码查询商品库存.har）：
- 修改: POST marketplace.wildberries.ru/ns/marketplace-app/marketplace-remote-wh/api/v3/portal/stocks/{warehouseId}
        body {"data":[{"chrtId":X,"amount":N}, ...]}（数组批量，探针实测 1000 条/请求通过）
- 查询: GET  同域 /api/v3/portal/stocks?order=asc&next=<游标>[&search=<vendorCode>]

定位：BCS stock 通道（stock/batchSetByChrtIdsBatch）的替代候选，BCS 通道保留不动；
后续看情况切换。安全机制与 BCS stock 完全一致：默认 dry-run、--apply 执行、
库存归零（amount=0）视为不可逆需 --yes、严禁写后自动 fetch 验证（--sync 才触发）。

chrtId 解析两条路径（--resolve）：
- snapshot（默认）：BCS 快照 sizeList[].chrtId（ops_plan.plan_stock 现成链路，零请求）
- live：WB portal/stocks 全量游标分页建 vendorCode→chrtId 实时映射（绕过快照滞后）

用法：
  wb.py stock-wb --name 短直假发 --amount 0                 # 全店 dry-run
  wb.py stock-wb --name 短直假发 --amount 0 --apply --yes   # 全店归零（不可逆）
  wb.py stock-wb --vc BCS-XXXX-XXXX --shops 9352 --amount 50 --apply
"""
import csv
import os
import time
from datetime import datetime

from wb_ops.adapters import bcs_client as bcs
from wb_ops.adapters import wb_client as wb_api
from wb_ops.adapters import wb_stock_client as wsc
from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from . import ops
from .ops_plan import (
    RED, GREEN, YELLOW, RESET,
    load_state,
    resolve_filters,
    plan_stock,
    SkipTracker,
    dry_run,
)
from .ops_executor import (
    RESULT_CSV,
    _normalize_csv_encoding,
    _rotate_result_csv_if_needed,
)

# portal stocks POST 数组单批条数：官方同构端点上限 1000，_scratch 探针实测 1000 通过，留边际取 500
STOCK_CHUNK = 500
BATCH_SLEEP = 0.3
SHOP_SLEEP = 0.6
ACTION_TAG = "stock_wb"   # ops_result.csv 操作列标签（与 BCS 通道的 "stock" 区分，便于追溯）


# ---------- 目标圈定 ----------

def _filter_shops(args):
    """--shops 过滤（照抄 ops.run 店铺过滤逻辑）。返回过滤后的 shops 列表。"""
    all_shops = ops.get_shops()
    if not all_shops:
        print("[错误] 未找到任何店铺数据文件，请先运行 wb.py fetch")
        return None
    shops = [int(s) for s in args.shops.split(",")] if getattr(args, "shops", None) else all_shops
    missing = [s for s in shops if s not in all_shops]
    if missing:
        print(f"[警告] 以下店铺无数据文件（未 fetch）：{missing}，将跳过")
        shops = [s for s in shops if s in all_shops]
    return shops


# ---------- 计划构建 ----------

def _live_stock_map(session, max_pages):
    """portal/stocks 全量游标分页 → {article: [(chrtId, amount), ...]}（实时 vendorCode→chrtId）。"""
    stocks = wsc.get_stocks(session, verbose=True, max_pages=max_pages)
    mapping = {}
    for s in stocks:
        art = s.get("article")
        chrt = s.get("chrtId")
        if not art or not chrt:
            continue
        mapping.setdefault(str(art), []).append((chrt, int(s.get("amount") or 0)))
    return mapping


def build_plans(vcs, shops, state, amount, resolve="snapshot", max_pages=200):
    """逐店组装改库存计划。resolve 见模块 docstring。产物结构与 ops_plan.plan_stock 一致。"""
    if resolve == "snapshot":
        return plan_stock(vcs, shops, state, amount)

    # live：WB 实时列表解析 chrtId（每店拉一次全量映射）
    cred = credentials.get()
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        shop = cred.wb_shop(sid)
        if not shop:
            for vc in vcs:
                tracker.add(sid, vc, "无 WB 凭证（credentials.json wb.shops 缺失/三件套不齐）")
            continue
        wh = bcs.default_warehouse_id(sid)
        if wh is None:
            print(f"  [警告] 店{sid} 未找到默认仓库「{config.DEFAULT_WAREHOUSE_NAME}」，该店库存操作跳过")
            continue
        try:
            session = wb_api.make_session(shop, cred.root_version)
            mapping = _live_stock_map(session, max_pages)
        except common.CookieExpiredError as e:
            print(f"  [警告] 店{sid} cookie 失效，跳过: {e}")
            for vc in vcs:
                tracker.add(sid, vc, "cookie 失效")
            continue
        items = []
        for vc in vcs:
            specs = mapping.get(vc) or []
            if not specs:
                tracker.add(sid, vc, "WB 实时列表无此规格")
                continue
            for chrt, cur_amt in specs:
                items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                              "chrtId": chrt, "warehouseId": wh, "amount": amount,
                              "orig_zero": (cur_amt == 0), "cur_amt": cur_amt,
                              "wh_fallback": False})
        if items:
            plans.append({"shopId": sid, "items": items,
                          "warehouses": _group_warehouses(items)})
    if tracker.count:
        tracker.report(f"设库存(WB实时) {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


def _group_warehouses(items):
    """items → [{"warehouseId", "stockItems": [{chrtId, amount}]}]（与 ops_plan._group_warehouses 同构）"""
    by_wh = {}
    for it in items:
        by_wh.setdefault(it["warehouseId"], {})[it["chrtId"]] = it["amount"]
    return [{"warehouseId": wh, "stockItems": [{"chrtId": c, "amount": a} for c, a in sorted(d.items())]}
            for wh, d in sorted(by_wh.items())]


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
                msg = "OK" if ok_flag else (r.get("errorText") or "error=true")
            except common.CookieExpiredError:
                raise
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
                results.append([sid, it["chrtId"], ACTION_TAG, it["amount"], msg, ts])
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


def _append_csv(results):
    """明细追加 ops_result.csv（复用 BCS 通道的编码归一化与按月归档）。"""
    _normalize_csv_encoding()
    _rotate_result_csv_if_needed()
    need_header = not (os.path.exists(RESULT_CSV) and os.path.getsize(RESULT_CSV) > 0)
    os.makedirs(os.path.dirname(RESULT_CSV), exist_ok=True)
    with open(RESULT_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(["店铺", "ID(nmId/chrtId)", "操作", "目标值", "接口响应", "时间"])
        w.writerows(results)


# ---------- CLI 主入口 ----------

def run(args):
    if getattr(args, "amount", 0) < 0:
        print("[错误] --amount 不能为负数")
        return 1

    vcs, desc = resolve_filters(args)
    if not vcs:
        print("[提示] 筛选结果为空，无操作")
        return 0
    print(f"目标: {desc}（{len(vcs)} 个 vendorCode）· 通道=WB 原生在线接口 · 解析={getattr(args, 'resolve', 'snapshot')}")

    shops = _filter_shops(args)
    if shops is None:
        return 1

    state, _, boss = load_state()
    plans = build_plans(vcs, shops, state, getattr(args, "amount", 0),
                        resolve=getattr(args, "resolve", "snapshot"),
                        max_pages=getattr(args, "max_pages", 200))
    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法操作（上方 [跳过] / [跳过汇总] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / sizeList 无 chrtId / 无 WB 凭证。")
        return 0

    if not getattr(args, "apply", False):
        dry_run(plans, "stock", getattr(args, "amount", None))
        return 0

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

    _append_csv(results)
    print(f"\n结果: 成功 {ok} · 失败 {fail}（明细已保存 {RESULT_CSV}）")
    _report_zero_items(plans)

    if getattr(args, "sync", False):
        from wb_ops.services.catalog_svc import catalog_svc
        catalog_svc.post_write_merge()
    else:
        common.print_write_hint()
    return 0


# ---------- 规范门面（供其他脚本依赖；跨域调用走 replicate_svc.set_stock_wb） ----------

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


def get_stock(shop, warehouse_id=None, *, search="", max_pages=200, verbose=False) -> list:
    """只读查询 WB 仓库库存条目（v3 游标分页），供手动核数（替代被禁止的写后自动验证）。

    warehouse_id=None 时拉账号级；search 传 vendorCode 精确匹配（HAR3 实证）。
    """
    session = wb_api.make_session(shop)
    return wsc.get_stocks(session, store_id=warehouse_id, search=search,
                          max_pages=max_pages, verbose=verbose)

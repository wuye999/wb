# -*- coding: utf-8 -*-
"""
wb_ops 一键操作框架：按映射表批量改价 / 改库存 / 下架（原 ops.py）

安全机制：默认 dry-run；--apply 执行；trash / 库存归零 需 --yes 确认。
数据前提：操作前先 wb.py fetch（从各店 JSON 定位 nmId/chrtId/warehouseId）。
"""
import argparse
import csv
import glob
import json
import os
import shutil
import sys
import time
from datetime import datetime

from . import bcs
from . import config
from . import mapping
from . import products
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

PRICE_CHUNK = 300
STOCK_CHUNK = 200
BATCH_SLEEP = 0.15
SHOP_SLEEP = 0.6
RESULT_CSV = config.RESULT_CSV

_shop_warehouses_cache = {}
_shop_rows_cache = {}


def get_shops():
    """扫描 data/products/ 下 shop{id}_products_all.json → [店id]"""
    return products.shop_ids_from_disk()


def load_shop_rows(sid):
    """读某店 JSON → {vc: row}（在架缓存）；文件缺失返回 None"""
    if sid in _shop_rows_cache:
        return _shop_rows_cache[sid]
    p = config.shop_json_path(sid)
    if not os.path.exists(p):
        print(f"  [跳过] 店{sid} 无数据文件 {os.path.basename(p)}，请先运行 wb.py fetch")
        _shop_rows_cache[sid] = None
        return None
    d = json.load(open(p, encoding="utf-8"))
    rows = {r.get("vendorCode"): r for r in d.get("rows", []) if not r.get("trashedAt")}
    _shop_rows_cache[sid] = rows
    return rows


def _shop_warehouses(sid):
    if sid in _shop_warehouses_cache:
        return _shop_warehouses_cache[sid]
    try:
        whs = bcs.fetch_warehouses(sid)
        out = [{"warehouseId": w.get("id"), "name": w.get("name", "")} for w in whs if w.get("id")]
        _shop_warehouses_cache[sid] = out
        return out
    except Exception as e:
        print(f"  [警告] 店{sid} 仓库列表获取失败：{e}")
        _shop_warehouses_cache[sid] = []
        return []


def load_state():
    """映射表状态 + 商品价格表商品（一次加载）"""
    state, excluded = mapping.load_mapping_state()
    boss = mapping.load_boss()
    return state, excluded, boss


# ---------------- 筛选 ----------------
def resolve_filters(args):
    """筛选参数 → (vcs, desc)。互斥；都不传 = 全部。"""
    state, _, boss = load_state()
    if args.vc:
        vcs = [v.strip() for v in args.vc.split(",") if v.strip()]
        miss = [v for v in vcs if v not in state]
        if miss:
            print(f"[警告] 以下 vendorCode 不在映射表中：{miss}（仍将尝试执行，可能查不到 nmId）")
        return vcs, f"vc 列表({len(vcs)} 个)"

    if args.sku or args.name or args.prefix:
        matched_boss = []
        for b in boss:
            if args.sku and b["sku"] == args.sku:
                matched_boss.append(b)
            elif args.name and args.name in (b["cn"] or ""):
                matched_boss.append(b)
            elif args.prefix and b.get("prefix") == args.prefix:
                matched_boss.append(b)
        if not matched_boss:
            print(f"[错误] 商品价格表中未匹配到商品（sku={args.sku} name={args.name} prefix={args.prefix}）")
            sys.exit(1)
        cns = {b["cn"] for b in matched_boss}
        vcs = sorted(vc for vc, st in state.items() if st["cn"] in cns)
        desc = " / ".join(f"{b['cn']}({b['sku']})" for b in matched_boss)
        return vcs, f"商品价格表商品: {desc}"

    return sorted(state.keys()), "全部映射商品"


def target_price(vc, state, boss, manual=None):
    """目标价：--price 手动 > 商品价格表 floor(双倍售价) > 映射表双倍售价 floor"""
    if manual is not None:
        return int(manual)
    cn = state.get(vc, {}).get("cn", "")
    b = next((x for x in boss if x["cn"] == cn), None)
    if b and b.get("floor") is not None:
        return b["floor"]
    dp = state.get(vc, {}).get("dp")
    if dp is not None:
        return int(dp)
    return None


# ---------------- 跳过统计 ----------------
class SkipTracker:
    def __init__(self):
        self.items = []

    def add(self, sid, vc, reason):
        self.items.append((sid, vc, reason))

    @property
    def count(self):
        return len(self.items)

    def report(self, action_label, show_detail=True):
        if not self.items:
            return
        from collections import Counter
        reasons = Counter(x[2] for x in self.items)
        print(f"\n{RED}⚠ [跳过汇总] {action_label}：共 {len(self.items)} 条未操作{RESET}")
        for reason, n in reasons.most_common():
            print(f"  {YELLOW}{reason}{RESET}：{n} 条")
        if show_detail:
            shown = self.items[:30]
            for sid, vc, reason in shown:
                print(f"    [跳过] 店{sid} {vc} → {reason}")
            if len(self.items) > 30:
                print(f"    ... 其余 {len(self.items) - 30} 条略（已计入上方分类统计）")
        else:
            print(f"  （明细略；共 {len(self.items)} 条）")
        return self.items


PRICE_HALF_LIMIT_NOTE = "低于原价一半（WB 将静默拒绝），已跳过"


# ---------------- 组装计划 ----------------
def plan_price(vcs, shops, state, boss, manual, discount, club, keep_price=False):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = load_shop_rows(sid)
        if rows is None:
            continue
        items = []
        for vc in vcs:
            r = rows.get(vc)
            if not r:
                tracker.add(sid, vc, "该店无此商品（未在架或未同步）")
                continue
            nm_id = r.get("nmId")
            if not nm_id:
                print(f"  [跳过] 店{sid} {vc} nmId 为空（ERROR 残留？），无法改价")
                tracker.add(sid, vc, "nmId 为空")
                continue
            sl = r.get("sizeList") or []
            cur = sl[0].get("price") if sl else None
            orig_zero = (cur is not None and float(cur) == 0)
            if keep_price:
                if cur is None or float(cur) == 0:
                    print(f"  [跳过] 店{sid} {vc} 当前价格 {cur}（0/空=WB 官方延迟或受限），无法保持价格改折扣（可普通改价）")
                    tracker.add(sid, vc, f"当前价格 {cur} 无法 keep-price")
                    continue
                p = int(float(cur))
            else:
                p = target_price(vc, state, boss, manual)
                if p is None:
                    print(f"  [跳过] 店{sid} {vc} 无目标价（商品价格表无该商品且映射表双倍售价为空），用 --price 指定")
                    tracker.add(sid, vc, "无目标价")
                    continue
            items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                          "nmID": nm_id, "price": p, "cur_price": cur,
                          "orig_zero": orig_zero,
                          "discount": discount, "clubDiscount": club})
        if items:
            plans.append({"shopId": sid, "items": items,
                          "dataList": [{"nmID": it["nmID"], "price": it["price"],
                                        "discount": it["discount"], "clubDiscount": it["clubDiscount"]}
                                       for it in items]})
    if tracker.count:
        tracker.report(f"改价 {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


def price_limit_violations(items):
    bad = []
    for it in items:
        cur = it.get("cur_price")
        if cur is None or it.get("price") is None:
            continue
        try:
            cur = float(cur)
        except (TypeError, ValueError):
            continue
        if it["price"] <= cur / 2:
            bad.append((it["vc"], it.get("cn", ""), cur, it["price"]))
    return bad


def plan_stock(vcs, shops, state, amount):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = load_shop_rows(sid)
        if rows is None:
            continue
        items = []
        for vc in vcs:
            r = rows.get(vc)
            if not r:
                tracker.add(sid, vc, "该店无此商品（未在架或未同步）")
                continue
            sl = r.get("sizeList") or []
            if not sl:
                print(f"  [跳过] 店{sid} {vc} 无规格数据（sizeList 空，chrtId 缺失），无法设库存")
                tracker.add(sid, vc, "无规格数据（chrtId 缺失）")
                continue
            for s in sl:
                chrt = s.get("chrtId")
                if not chrt:
                    print(f"  [跳过] 店{sid} {vc} 规格缺少 chrtId，无法设库存")
                    tracker.add(sid, vc, "规格缺少 chrtId")
                    continue
                stocks = s.get("stockList") or []
                cur_amt = sum((st.get("amount") or 0) for st in stocks) if stocks else 0
                if not stocks:
                    whs = _shop_warehouses(sid)
                    if not whs:
                        print(f"  [跳过] 店{sid} {vc} 规格 chrtId={chrt} 无仓库记录且店铺无仓库列表，无法设库存")
                        tracker.add(sid, vc, f"无仓库记录且店铺无仓库列表(chrtId={chrt})")
                        continue
                    wh = whs[0]["warehouseId"]
                    items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                                  "chrtId": chrt, "warehouseId": wh, "amount": amount,
                                  "orig_zero": (cur_amt == 0), "cur_amt": cur_amt, "wh_fallback": True})
                    continue
                for st in stocks:
                    wh = st.get("warehouseId")
                    if wh is not None:
                        items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                                      "chrtId": chrt, "warehouseId": wh, "amount": amount,
                                      "orig_zero": (cur_amt == 0), "cur_amt": cur_amt})
                    else:
                        print(f"  [跳过] 店{sid} {vc} 规格 chrtId={chrt} 仓库记录缺少 warehouseId，无法设库存")
                        tracker.add(sid, vc, f"仓库记录缺少 warehouseId(chrtId={chrt})")
        if items:
            plans.append({"shopId": sid, "items": items,
                          "warehouses": _group_warehouses(items)})
    if tracker.count:
        tracker.report(f"设库存 {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


def _group_warehouses(items):
    by_wh = {}
    for it in items:
        by_wh.setdefault(it["warehouseId"], {})[it["chrtId"]] = it["amount"]
    return [{"warehouseId": wh, "stockItems": [{"chrtId": c, "amount": a} for c, a in sorted(d.items())]}
            for wh, d in sorted(by_wh.items())]


def plan_trash(vcs, shops, state):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = load_shop_rows(sid)
        if rows is None:
            continue
        items = []
        for vc in vcs:
            r = rows.get(vc)
            if not r:
                tracker.add(sid, vc, "该店无此商品（未在架或未同步）")
                continue
            nm_id = r.get("nmId")
            if not nm_id:
                print(f"  [跳过] 店{sid} {vc} nmId 为空（ERROR 残留，API 无法删除），跳过")
                tracker.add(sid, vc, "nmId 为空")
                continue
            specs = []
            sl = r.get("sizeList") or []
            if sl:
                for s in sl:
                    chrt = s.get("chrtId")
                    if not chrt:
                        continue
                    stocks = s.get("stockList") or []
                    if stocks:
                        for st in stocks:
                            wh = st.get("warehouseId")
                            if wh is not None:
                                specs.append((chrt, wh))
                    else:
                        whs = _shop_warehouses(sid)
                        if whs:
                            specs.append((chrt, whs[0]["warehouseId"]))
            if not specs:
                print(f"  [提示] 店{sid} {vc} 无库存规格（sizeList 空或仓库缺失），下架前无法清库存")
            items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                          "nmID": nm_id, "stock_specs": specs})
        if items:
            plans.append({"shopId": sid, "items": items,
                          "nmIds": [it["nmID"] for it in items]})
    if tracker.count:
        tracker.report(f"下架 {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


# ---------------- dry-run / 执行 ----------------
def dry_run(plans, action, amount=None):
    print(f"\n{YELLOW}===== 计划清单（dry-run，未执行）====={RESET}")
    total = 0
    for p in plans:
        print(f"\n店{p['shopId']}（{len(p['items'])} 个商品）:")
        for it in p["items"]:
            if action == "price":
                target = f"price={it['price']}"
                if it.get("discount") is not None:
                    target += f", discount={it['discount']}"
                if it.get("clubDiscount") is not None:
                    target += f", clubDiscount={it['clubDiscount']}"
            elif action == "stock":
                target = f"stock={it['amount']}（chrtId={it['chrtId']}, warehouse={it['warehouseId']}"
                if it.get("wh_fallback"):
                    target += f"{YELLOW}·店铺默认仓库{RESET}"
                target += ")"
            else:
                target = f"移回收站（nmId={it['nmID']}"
                specs = it.get("stock_specs") or []
                target += f"{YELLOW}·先清库存{len(specs)}规格→0{RESET}" if specs else f"{YELLOW}·无库存规格可清{RESET}"
                target += ")"
            if action == "price" and price_limit_violations([it]):
                target += f"  {RED}⚠ {PRICE_HALF_LIMIT_NOTE}{RESET}"
            if it.get("orig_zero"):
                target += f"  {YELLOW}⚠ 当前值为 0（WB 延迟/受限），照常修改；复查仍 0 属 WB 官方原因{RESET}"
            print(f"  {it['vc']} | {it['cn']} | {target}")
            total += 1
    print(f"\n合计 {total} 条操作（{len(plans)} 个店铺）。加 --apply 真正执行。")


def _normalize_csv_encoding():
    if not os.path.exists(RESULT_CSV) or os.path.getsize(RESULT_CSV) == 0:
        return
    try:
        with open(RESULT_CSV, "r", encoding="utf-8-sig") as f:
            f.read()
        return
    except UnicodeDecodeError:
        pass
    raw = open(RESULT_CSV, "rb").read().splitlines()
    lines = []
    for ln in raw:
        try:
            lines.append(ln.decode("utf-8-sig"))
        except UnicodeDecodeError:
            lines.append(ln.decode("gb18030"))
    os.makedirs(os.path.dirname(RESULT_CSV), exist_ok=True)
    shutil.copy2(RESULT_CSV, RESULT_CSV + ".bak")
    with open(RESULT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"  ⚠ ops_result.csv 非 UTF-8 编码（可能被 Excel 另存过），已备份 {RESULT_CSV}.bak 并转码为 UTF-8-SIG")


def run_apply(plans, action):
    ok = fail = 0
    skipped_bad = 0
    zero_items = []
    results = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pi, p in enumerate(plans):
        sid = p["shopId"]
        print(f"\n>>> 店{sid}：{action}")
        if action == "price":
            bad = price_limit_violations(p["items"])
            if bad:
                print(f"  {RED}[跳过] {len(bad)} 条{PRICE_HALF_LIMIT_NOTE}：{RESET}")
                for vc, cn, cur, target in bad[:10]:
                    print(f"    {vc} | {cn} | 原价 {cur} → 目标 {target}")
                if len(bad) > 10:
                    print(f"    ... 共 {len(bad)} 条")
                skipped_bad += len(bad)
            ok_items = [it for it in p["items"] if not any(it["vc"] == b[0] and it["price"] == b[3] for b in bad)]
            zero_items.extend((sid, it["vc"], it.get("cn", ""), it.get("cur_price"), it["price"])
                              for it in ok_items if it.get("orig_zero"))
            dl = [{"nmID": it["nmID"], "price": it["price"],
                   "discount": it["discount"], "clubDiscount": it["clubDiscount"]} for it in ok_items]
            if not dl:
                print("  本店无有效改价项，跳过")
                continue
            for i in range(0, len(dl), PRICE_CHUNK):
                body = {"shopId": sid, "dataList": dl[i:i + PRICE_CHUNK]}
                ok_flag, msg = _post(body, f"{action} 批{i // PRICE_CHUNK + 1}")
                if ok_flag:
                    ok += len(body["dataList"])
                else:
                    fail += len(body["dataList"])
                for it in body["dataList"]:
                    results.append([sid, it["nmID"], action, it["price"], msg, ts])
                if i + PRICE_CHUNK < len(dl):
                    time.sleep(BATCH_SLEEP)
        elif action == "stock":
            for wh in p["warehouses"]:
                sis = wh["stockItems"]
                for i in range(0, len(sis), STOCK_CHUNK):
                    body = {"shopId": sid,
                            "warehouses": [{"warehouseId": wh["warehouseId"],
                                            "stockItems": sis[i:i + STOCK_CHUNK]}]}
                    ok_flag, msg = _post(body, f"stock 仓库{wh['warehouseId']} 批{i // STOCK_CHUNK + 1}")
                    if ok_flag:
                        ok += len(sis[i:i + STOCK_CHUNK])
                    else:
                        fail += len(sis[i:i + STOCK_CHUNK])
                    for it in sis[i:i + STOCK_CHUNK]:
                        results.append([sid, it["chrtId"], action, it["amount"], msg, ts])
                    if i + STOCK_CHUNK < len(sis):
                        time.sleep(BATCH_SLEEP)
            zero_items.extend((sid, it["vc"], it.get("cn", ""), it.get("cur_amt"), it["amount"])
                              for it in p["items"] if it.get("orig_zero"))
        elif action == "trash":
            by_wh = {}
            spec_total = 0
            for it in p["items"]:
                for chrt, wh in it.get("stock_specs", []):
                    by_wh.setdefault(wh, []).append(chrt)
            clear_fail = []
            for wh, chrt_list in sorted(by_wh.items()):
                for i in range(0, len(chrt_list), STOCK_CHUNK):
                    body = {"shopId": sid,
                            "warehouses": [{"warehouseId": wh,
                                            "stockItems": [{"chrtId": c, "amount": 0}
                                                           for c in chrt_list[i:i + STOCK_CHUNK]]}]}
                    ok_flag, _msg = _post(body, f"清库存 仓库{wh} 批{i // STOCK_CHUNK + 1}")
                    if ok_flag:
                        spec_total += len(chrt_list[i:i + STOCK_CHUNK])
                    else:
                        clear_fail.extend(chrt_list[i:i + STOCK_CHUNK])
            if by_wh:
                print(f"  [清库存] 下架前已提交 {spec_total} 个规格库存归零" +
                      (f"，{len(clear_fail)} 个失败" if clear_fail else ""))
            nms = p["nmIds"]
            for i in range(0, len(nms), PRICE_CHUNK):
                body = {"shopId": sid, "nmIds": nms[i:i + PRICE_CHUNK]}
                ok_flag, msg = _post(body, f"trash 批{i // PRICE_CHUNK + 1}")
                if ok_flag:
                    ok += len(body["nmIds"])
                else:
                    fail += len(body["nmIds"])
                for nm in body["nmIds"]:
                    results.append([sid, nm, action, "", msg, ts])
                if i + PRICE_CHUNK < len(nms):
                    time.sleep(BATCH_SLEEP)
            if clear_fail:
                print(f"  {RED}⚠ [清库存失败] {len(clear_fail)} 个规格库存未清零仍已下架（可能 WB 受限/延迟）{RESET}")
                print(f"    chrtId: {clear_fail[:10]}{'...' if len(clear_fail) > 10 else ''}")
                results.extend([sid, c, "stock_clear_fail", 0, "FAIL", ts] for c in clear_fail)
        if pi < len(plans) - 1:
            time.sleep(SHOP_SLEEP)

    _normalize_csv_encoding()
    need_header = not (os.path.exists(RESULT_CSV) and os.path.getsize(RESULT_CSV) > 0)
    os.makedirs(os.path.dirname(RESULT_CSV), exist_ok=True)
    with open(RESULT_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(["店铺", "ID(nmId/chrtId)", "操作", "目标值", "接口响应", "时间"])
        w.writerows(results)
    summary = f"结果: 成功 {ok} · 失败 {fail}"
    if skipped_bad:
        summary += f" · {RED}价格下限剔除 {skipped_bad}{RESET}"
    print(f"\n{summary}（明细已保存 {RESULT_CSV}）")

    if zero_items:
        print(f"\n{RED}⚠ [0 值商品报告] {len(zero_items)} 项操作前价格/库存为 0（WB 官方数据延迟或商品受限）{RESET}")
        print("  已照常提交修改；若之后查询仍为 0，属 WB 官方原因（修改生效可能延迟），无需反复修改。")
        seen = {}
        for sid, vc, cn, old, new in zero_items:
            seen.setdefault(vc, {"cn": cn, "shops": [], "old": old, "new": new})["shops"].append(sid)
        for vc, d in sorted(seen.items()):
            print(f"    {vc} | {d['cn']} | 原值 {d['old']} → 目标 {d['new']} | 店 {','.join(map(str, d['shops']))}")
    return ok, fail


def _post(body, label):
    """提交写操作，返回 (ok: bool, msg: str)。ok=False 表示接口失败/异常，供 run_apply 准确计数。"""
    try:
        r = bcs.http_post_json(f"{bcs.base_url()}/shopKeeper/{_endpoint(body)}", body)
        msg = r.get("msg", "")
        if r.get("code") == 200:
            print(f"  {GREEN}✓{RESET} {label} 成功: {msg}")
            return True, (msg or "OK")
        print(f"  {RED}✗{RESET} {label} 失败: code={r.get('code')} {msg}")
        return False, f"FAIL: {msg}"
    except Exception as e:
        print(f"  {RED}✗{RESET} {label} 异常: {e}")
        return False, f"FAIL: {e}"


def _endpoint(body):
    if "dataList" in body:
        return "price/batch"
    if "warehouses" in body:
        return "stock/batchSetByChrtIdsBatch"
    if "nmIds" in body:
        return "clean/removeToTrash"
    raise RuntimeError("无法识别操作类型")


def confirm_irreversible(action, amount, yes):
    danger = action == "trash" or (action == "stock" and amount == 0)
    if not danger:
        return True
    if yes:
        return True
    tip = "移至回收站" if action == "trash" else f"库存归零(amount={amount})"
    print(f"\n{RED}⚠ 即将执行不可逆操作：{tip}！{RESET}")
    try:
        ans = input(f"输入 y 确认执行，其他任意键取消: ").strip().lower()
        return ans == "y"
    except EOFError:
        return False


# ---------------- 入口（供 cli 调用） ----------------
def run(action, args):
    for d in ("discount", "club_discount"):
        v = getattr(args, d, None)
        if v is not None and not (0 <= v <= 100):
            print(f"[错误] {d} 需在 0-100 之间")
            sys.exit(1)

    vcs, desc = resolve_filters(args)
    if not vcs:
        print("[提示] 筛选结果为空，无操作")
        return
    print(f"目标: {desc}（{len(vcs)} 个 vendorCode）")

    all_shops = get_shops()
    if not all_shops:
        print("[错误] 未找到任何店铺数据文件，请先运行 wb.py fetch")
        sys.exit(1)
    shops = [int(s) for s in args.shops.split(",")] if args.shops else all_shops
    missing = [s for s in shops if s not in all_shops]
    if missing:
        print(f"[警告] 以下店铺无数据文件（未 fetch）：{missing}，将跳过")
        shops = [s for s in shops if s in all_shops]

    state, _, boss = load_state()

    if action == "price":
        manual = getattr(args, "price", None)
        plans = plan_price(vcs, shops, state, boss, manual, args.discount, args.club_discount,
                           keep_price=args.keep_price)
    elif action == "stock":
        plans = plan_stock(vcs, shops, state, args.amount)
    else:
        plans = plan_trash(vcs, shops, state)

    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法操作（上方 [跳过] / [跳过汇总] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / sizeList 无 chrtId。")
        return

    if not args.apply:
        dry_run(plans, action, getattr(args, "amount", None))
        return

    if not confirm_irreversible(action, getattr(args, "amount", 0), args.yes):
        print("已取消，未执行任何操作")
        return

    run_apply(plans, action)


# 供 argparse 复用：给 price/stock/trash 三个子命令加筛选与通用参数
def add_ops_args(p, *, with_price=False, with_stock=False):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--sku", help="商品价格表卖家SKU")
    g.add_argument("--name", help="商品价格表产品中文名包含")
    g.add_argument("--prefix", help="商品价格表 vendorCode 前缀码")
    g.add_argument("--vc", help="vendorCode 列表（逗号分隔）")
    g.add_argument("--all", action="store_true", help="全部映射商品（默认）")
    p.add_argument("--shops", help="限定店铺ID（逗号分隔，默认全部已 fetch 店铺）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--yes", action="store_true", help="跳过不可逆操作确认")
    if with_price:
        p.add_argument("--price", type=int, help="目标价（默认 floor(商品价格表双倍售价)）")
        p.add_argument("--discount", type=int, help="折扣 0-100（不传=不改）")
        p.add_argument("--club-discount", type=int, help="club折扣 0-100（不传=不改）")
        p.add_argument("--keep-price", action="store_true", help="价格保持当前值（只改折扣/俱乐部折扣）")
    if with_stock:
        p.add_argument("--amount", type=int, default=0, help="目标库存（默认 0）")

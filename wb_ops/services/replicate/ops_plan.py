# -*- coding: utf-8 -*-
"""
wb_ops 操作计划生成与 dry-run 模块 (ops_plan)
负责命令行筛选条件解析、目标价格与库存计算、跳过统计与变更计划构建。
"""
import sys
from collections import Counter
from wb_ops.adapters import bcs_client as bcs
from wb_ops import config
from wb_ops.storage.product_repo import ProductSnapshotRepository
from wb_ops.storage.mapping_repo import MappingRepository

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

PRICE_HALF_LIMIT_NOTE = "低于原价一半（WB 将静默拒绝），已跳过"


def load_shop_rows(sid):
    """读某店 JSON → {vc: row}（在架缓存）；文件缺失返回 None"""
    res = ProductSnapshotRepository.load_shop_rows(sid)
    if res is None:
        p = config.shop_json_path(sid)
        print(f"  [跳过] 店{sid} 无数据文件 {p}，请先运行 wb.py fetch")
    return res


def load_state():
    """映射表状态 + 商品价格表商品（一次加载）"""
    state, excluded = MappingRepository.load_mapping_state()
    boss = MappingRepository.load_boss()
    return state, excluded, boss



def resolve_filters(args):
    """筛选参数 → (vcs, desc)。互斥；都不传 = 全部。"""
    state, _, boss = load_state()
    if getattr(args, "vc", None):
        vcs = [v.strip() for v in args.vc.split(",") if v.strip()]
        miss = [v for v in vcs if v not in state]
        if miss:
            print(f"[警告] 以下 vendorCode 不在映射表中：{miss}（仍将尝试执行，可能查不到 nmId）")
        return vcs, f"vc 列表({len(vcs)} 个)"

    if getattr(args, "sku", None) or getattr(args, "name", None) or getattr(args, "prefix", None):
        matched_boss = []
        for b in boss:
            if getattr(args, "sku", None) and b["sku"] == args.sku:
                matched_boss.append(b)
            elif getattr(args, "name", None) and args.name in (b["cn"] or ""):
                matched_boss.append(b)
            elif getattr(args, "prefix", None) and b.get("prefix") == args.prefix:
                matched_boss.append(b)
        if not matched_boss:
            print(f"[错误] 商品价格表中未匹配到商品（sku={getattr(args, 'sku', None)} name={getattr(args, 'name', None)} prefix={getattr(args, 'prefix', None)}）")
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


def price_review_items(items):
    """返回降价落在 (30%, 50%] 的商品（进入价格审查，需「应用新价格」）。
    判断：0.5*cur < price <= 0.7*cur（cur=原价，price=新价）。"""
    review = []
    for it in items:
        cur = it.get("cur_price")
        if cur is None or it.get("price") is None:
            continue
        try:
            cur = float(cur)
        except (TypeError, ValueError):
            continue
        if cur <= 0:
            continue
        price = it["price"]
        if cur * 0.5 < price <= cur * 0.7:
            review.append(it)
    return review


def _group_warehouses(items):
    by_wh = {}
    for it in items:
        by_wh.setdefault(it["warehouseId"], {})[it["chrtId"]] = it["amount"]
    return [{"warehouseId": wh, "stockItems": [{"chrtId": c, "amount": a} for c, a in sorted(d.items())]}
            for wh, d in sorted(by_wh.items())]


def plan_stock(vcs, shops, state, amount):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = load_shop_rows(sid)
        if rows is None:
            continue
        default_wh = bcs.default_warehouse_id(sid)
        if default_wh is None:
            print(f"  [警告] 店{sid} 未找到默认仓库「{config.DEFAULT_WAREHOUSE_NAME}」，该店库存操作跳过")
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
                # 只操作默认仓库（莫斯科）；成都等其他仓库一律跳过
                moscow = [st for st in stocks if st.get("warehouseId") == default_wh]
                cur_amt = sum((st.get("amount") or 0) for st in moscow)
                items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                              "chrtId": chrt, "warehouseId": default_wh, "amount": amount,
                              "orig_zero": (cur_amt == 0), "cur_amt": cur_amt,
                              "wh_fallback": not moscow})
        if items:
            plans.append({"shopId": sid, "items": items,
                          "warehouses": _group_warehouses(items)})
    if tracker.count:
        tracker.report(f"设库存 {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


def plan_trash(vcs, shops, state):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = load_shop_rows(sid)
        if rows is None:
            continue
        default_wh = bcs.default_warehouse_id(sid)
        if default_wh is None:
            print(f"  [警告] 店{sid} 未找到默认仓库「{config.DEFAULT_WAREHOUSE_NAME}」，下架前将不清库存（仍会移回收站）")
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
            if sl and default_wh:
                for s in sl:
                    chrt = s.get("chrtId")
                    if not chrt:
                        continue
                    # 只清默认仓库（莫斯科）库存；成都等其他仓库不动
                    specs.append((chrt, default_wh))
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

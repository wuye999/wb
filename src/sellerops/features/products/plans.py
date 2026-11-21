"""
sellerops 操作计划生成与 dry-run 模块 (ops_plan)
负责命令行筛选条件解析、目标价格与库存计算、跳过统计与变更计划构建。
"""
import math
from sellerops.features.catalog.selection import load_shop_rows
from sellerops.features.products import warehouse
from sellerops.features.products.skip_report import SkipTracker
from sellerops.domain.snapshot_values import first_price, positive_id, stock_at

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

PRICE_HALF_LIMIT_NOTE = "低于原价一半（WB 将静默拒绝），已跳过"


def target_price(vc, state, boss, manual=None):
    """目标价：--price 手动 > 商品价格表 floor(实际售价) > 映射表实际售价快照 floor"""
    if manual is not None:
        return int(manual)
    cn = state.get(vc, {}).get("cn", "")
    b = next((x for x in boss if x["cn"] == cn), None)
    if b and b.get("floor") is not None:
        return b["floor"]
    dp = state.get(vc, {}).get("dp")
    if dp is not None:
        try:
            return math.floor(float(dp))
        except (TypeError, ValueError):
            return None
    return None


def report_blocked(plans: list[dict], tracker: SkipTracker, label: str) -> None:
    for plan in plans:
        for item in plan.get('blocked_items', []):
            tracker.add(plan['shopId'], item['vc'], item['reason'])
    tracker.report(label)


def plan_price(vcs, shops, state, boss, manual, discount, club, keep_price=False, *,
               resolver=None, allow_discount_only=False, shop_rows=None):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = shop_rows[sid] if shop_rows is not None else load_shop_rows(sid)
        if rows is None:
            plans.append({'shopId': sid, 'items': [], 'dataList': [], 'blocked_items': [
                _blocked_item(vc, {}, state, '本店快照缺失或不可读，无法定位商品') for vc in dict.fromkeys(vcs)]})
            continue
        items, blocked = [], []
        for vc in dict.fromkeys(vcs):
            r = rows.get(vc)
            if not r:
                tracker.add(sid, vc, "该店无此商品（未在架或未同步）")
                continue
            only_discount = keep_price and allow_discount_only and discount is not None
            if resolver:
                prepared = resolver.ensure(sid, vc, r, price=keep_price and not only_discount)
                if prepared.reason:
                    blocked.append(_blocked_item(vc, r, state, prepared.reason))
                    continue
                r = prepared.row
            nm_id = positive_id(r.get("nmId"))
            if nm_id is None:
                blocked.append(_blocked_item(vc, r, state, '缺少有效nmId，无法改价'))
                continue
            cur = first_price(r)
            orig_zero = (cur is not None and float(cur) == 0)
            if only_discount:
                p = None  # WB支持只传折扣，避免依赖或覆盖缺失/多规格的原价。
            elif keep_price:
                if cur is None or float(cur) == 0:
                    print(f"  [跳过] 店{sid} {vc} 当前价格 {cur}（0/空=WB 官方延迟或受限），无法保持价格改折扣（可普通改价）")
                    blocked.append(_blocked_item(vc, r, state, f"当前价格 {cur} 无法 keep-price"))
                    continue
                p = int(float(cur))
            else:
                p = target_price(vc, state, boss, manual)
                if p is None:
                    print(f"  [跳过] 店{sid} {vc} 无目标价（商品价格表无该商品且映射表实际售价为空），用 --price 指定")
                    blocked.append(_blocked_item(vc, r, state, '无目标价，请用--price指定'))
                    continue
            items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                          "nmID": nm_id, "price": p, "cur_price": cur,
                          "orig_zero": orig_zero,
                          "discount": discount, "clubDiscount": club})
        if resolver:
            resolver.block_after_auth_failure(sid, items, blocked)
        if items or blocked:
            plans.append({"shopId": sid, "items": items,
                          "blocked_items": blocked,
                          "dataList": [{"nmID": it["nmID"], "price": it["price"],
                                        "discount": it["discount"], "clubDiscount": it["clubDiscount"]}
                                       for it in items]})
    report_blocked(plans, tracker, f"改价 {len(vcs)} 个 vendorCode × {len(shops)} 店")
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


def _snapshot_specs(row: dict) -> tuple[list[int], str | None]:
    sizes = row.get("sizeList")
    if not isinstance(sizes, list) or not sizes:
        return [], "无完整规格数据（sizeList 为空或无效）"
    ids = [positive_id(s.get("chrtId")) if isinstance(s, dict) else None for s in sizes]
    if any(chrt is None for chrt in ids):
        return [], "存在缺失或无效的 chrtId，不能只清理部分规格"
    if len(set(ids)) != len(ids):
        return [], "存在重复的 chrtId，无法确认完整规格"
    return sorted(set(ids)), None


def trash_item_error(item: dict) -> str | None:
    """下架前必须有有效商品 ID、完整规格和单一目标仓库。"""
    if positive_id(item.get("nmID")) is None:
        return "缺少有效 nmId"
    specs = item.get("stock_specs")
    if not isinstance(specs, (list, tuple)) or not specs:
        return "缺少清库存规格或莫斯科仓库 ID"
    if any(not isinstance(s, (list, tuple)) or len(s) != 2
           or positive_id(s[0]) is None or positive_id(s[1]) is None for s in specs):
        return "清库存规格或仓库 ID 不完整"
    if len({int(s[1]) for s in specs}) != 1:
        return "下架清库存计划不能涉及多个仓库"
    return None


def _blocked_item(vc, row, state, reason):
    return {"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
            "nmID": row.get("nmId"), "reason": reason}


def plan_stock(vcs, shops, state, amount, *, resolver=None, shop_rows=None):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = shop_rows[sid] if shop_rows is not None else load_shop_rows(sid)
        if rows is None:
            plans.append({'shopId': sid, 'items': [], 'warehouses': [], 'blocked_items': [
                _blocked_item(vc, {}, state, '本店快照缺失或不可读，无法定位商品') for vc in dict.fromkeys(vcs)]})
            continue
        targets = [(vc, rows[vc]) for vc in dict.fromkeys(vcs) if vc in rows]
        for vc in dict.fromkeys(vcs):
            if vc not in rows:
                tracker.add(sid, vc, "该店无此商品（未在架或未同步）")
        if not targets:
            continue
        default_wh = warehouse.resolve_warehouse(sid)
        if default_wh is None:
            reason = "无法确认该店莫斯科仓库，已阻止库存操作"
            blocked = [_blocked_item(vc, r, state, reason) for vc, r in targets]
            plans.append({"shopId": sid, "items": [], "warehouses": [], "blocked_items": blocked})
            continue
        items, blocked = [], []
        for vc, r in targets:
            if resolver:
                prepared = resolver.ensure(sid, vc, r, nm=False, specs=True)
                if prepared.reason:
                    blocked.append(_blocked_item(vc, r, state, prepared.reason))
                    continue
                r = prepared.row
            spec_ids, reason = _snapshot_specs(r)
            if reason:
                blocked.append(_blocked_item(vc, r, state, reason))
                continue
            by_id = {int(s["chrtId"]): s for s in r["sizeList"]}
            for chrt in spec_ids:
                s = by_id[chrt]
                # 只读取已确认的本店莫斯科仓；缺失或重复库存记录不补零、不求和。
                cur_amt = stock_at(s, default_wh)
                items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                              "chrtId": chrt, "warehouseId": default_wh, "amount": amount,
                              "orig_zero": (cur_amt == 0), "cur_amt": cur_amt,
                              "wh_fallback": cur_amt is None})
        if resolver:
            resolver.block_after_auth_failure(sid, items, blocked)
        if items or blocked:
            plans.append({"shopId": sid, "items": items,
                          "warehouses": _group_warehouses(items), "blocked_items": blocked})
    report_blocked(plans, tracker, f"设库存 {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


def plan_trash(vcs, shops, state, *, resolver=None, shop_rows=None):
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = shop_rows[sid] if shop_rows is not None else load_shop_rows(sid)
        if rows is None:
            plans.append({'shopId': sid, 'items': [], 'nmIds': [], 'blocked_items': [
                _blocked_item(vc, {}, state, '本店快照缺失或不可读，无法定位商品') for vc in dict.fromkeys(vcs)]})
            continue
        targets = [(vc, rows[vc]) for vc in dict.fromkeys(vcs) if vc in rows]
        for vc in dict.fromkeys(vcs):
            if vc not in rows:
                tracker.add(sid, vc, "该店无此商品（未在架或未同步）")
        if not targets:
            continue
        default_wh = warehouse.resolve_warehouse(sid)
        if default_wh is None:
            reason = "无法确认该店莫斯科仓库，已阻止下架"
            blocked = [_blocked_item(vc, r, state, reason) for vc, r in targets]
            plans.append({"shopId": sid, "items": [], "nmIds": [], "blocked_items": blocked})
            continue
        items, blocked = [], []
        for vc, r in targets:
            if resolver:
                prepared = resolver.ensure(sid, vc, r, specs=True)
                if prepared.reason:
                    blocked.append(_blocked_item(vc, r, state, prepared.reason))
                    continue
                r = prepared.row
            spec_ids, reason = _snapshot_specs(r)
            item = {"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
                    "nmID": r.get("nmId"), "stock_specs": [(c, default_wh) for c in spec_ids]}
            reason = reason or trash_item_error(item)
            if reason:
                blocked.append(_blocked_item(vc, r, state, reason))
                continue
            item["nmID"] = int(item["nmID"])
            items.append(item)
        if resolver:
            resolver.block_after_auth_failure(sid, items, blocked)
        if items or blocked:
            plans.append({"shopId": sid, "items": items,
                          "nmIds": [it["nmID"] for it in items], "blocked_items": blocked})
    report_blocked(plans, tracker, f"下架 {len(vcs)} 个 vendorCode × {len(shops)} 店")
    return plans


def dry_run(plans, action, amount=None):
    print(f"\n{YELLOW}===== 计划清单（dry-run，未执行）====={RESET}")
    total = 0
    blocked = 0
    for p in plans:
        n_products = len({it["vc"] for it in p["items"]})
        specs = f"，{len(p['items'])} 个规格" if action == "stock" else ""
        print(f"\n店{p['shopId']}（{n_products} 个商品{specs}）:")
        for it in p["items"]:
            if action == "price":
                target = f"price={it['price']}" if it['price'] is not None else '保持各规格原价（不提交价格字段）'
                if it.get("discount") is not None:
                    target += f", discount={it['discount']}"
                if it.get("clubDiscount") is not None:
                    target += f", clubDiscount={it['clubDiscount']}"
            elif action == "stock":
                target = f"stock={it['amount']}（chrtId={it['chrtId']}, warehouse={it['warehouseId']}"
                if it.get("wh_fallback"):
                    target += f"{YELLOW}·店铺默认仓库{RESET}"
                if it.get('cur_amt') is None:
                    target += f"{YELLOW}·当前库存未知{RESET}"
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
        for it in p.get("blocked_items", []):
            print(f"  [已阻止] {it['vc']} | {it.get('cn', '')} | {it['reason']}")
            blocked += 1
    print(f"\n合计 {total} 条操作（{len(plans)} 个店铺）。加 --apply 真正执行。")
    if blocked:
        print(f"[失败] {blocked} 个商品的计划不完整，已阻止操作；请处理上方原因")

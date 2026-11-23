"""商品选择与提交计划；预览和执行共用字段及归属检查。"""
from collections import defaultdict

from sellerops.features.catalog.selection import load_shop_rows, selection_error
from sellerops.features.products import warehouse
from sellerops.features.products.skip_report import SkipTracker
from sellerops.domain.snapshot_values import bcs_keep_price, first_price, positive_id, stock_at, valid_sizes

RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def validate_price_intent(price, keep_price, discount, club_discount):
    if price is not None:
        if type(price) is not int or price <= 0:
            raise ValueError("目标价必须为正整数")
    elif not keep_price or (discount is None and club_discount is None):
        raise ValueError("必须使用 --price 指定目标价，或使用 --keep-price 搭配 --discount（BCS 可用 --club-discount）")


def report_blocked(plans: list[dict], tracker: SkipTracker, label: str) -> None:
    for plan in plans:
        for item in plan.get('blocked_items', []):
            tracker.add(plan['shopId'], item['vc'], item['reason'])
    tracker.report(label)


def report_plan_skips(plans: list[dict], label: str) -> None:
    tracker = SkipTracker()
    for plan in plans:
        if plan.get("range_error") and not plan["blocked_items"]:
            tracker.add(plan["shopId"], "", plan["range_error"])
        for item in plan["skipped_items"]:
            tracker.add(plan["shopId"], item["vc"], item["reason"])
    report_blocked(plans, tracker, label)
    if tracker.path:
        plans[0]["skip_report"] = tracker.path


def blocked_item(vc: str, row: dict, state: dict, reason: str) -> dict:
    return {"vc": vc, "cn": state.get(vc, {}).get("cn", ""),
            "nmID": row.get("nmId"), "reason": reason}


def shop_targets(sid: int, vcs: list[str], rows: dict | None, state: dict) -> tuple[dict, dict]:
    plan = {"shopId": sid, "items": [], "blocked_items": [], "skipped_items": []}
    targets = {}
    if rows is None:
        reason = "本店快照缺失或不可读，无法定位商品"
        plan["range_error"] = reason
        plan["blocked_items"] = [blocked_item(vc, {}, state, reason) for vc in dict.fromkeys(vcs)]
        return plan, targets
    for vc in dict.fromkeys(vcs):
        if vc not in rows:
            item = blocked_item(vc, {}, state, "该店无此商品（未在架或未同步）")
            plan["skipped_items"].append({**item, "present": False})
        elif reason := selection_error(vc, state):
            plan["blocked_items"].append(blocked_item(vc, rows[vc], state, reason))
        else:
            targets[vc] = rows[vc]
    return plan, targets


def prepare_cards(sid: int, targets: dict, rows: dict, state: dict, blocked: list[dict],
                  resolver, *, price: bool = False) -> dict:
    prepared = {}
    owners = defaultdict(set)
    for vc, row in (rows or {}).items():
        if nm := positive_id(row.get("nmId")):
            owners[nm].add(vc)
    for vc, row in targets.items():
        if resolver:
            entry = resolver.ensure(sid, vc, row, price=price)
            if entry.reason:
                blocked.append(blocked_item(vc, row, state, entry.reason))
                continue
            row = entry.row
        nm = positive_id(row.get("nmId"))
        if nm is None:
            blocked.append(blocked_item(vc, row, state, "缺少有效 nmId"))
            continue
        owners[nm].add(vc)
        prepared[vc] = row
    for vc, row in list(prepared.items()):
        if len(owners[positive_id(row["nmId"])]) > 1:
            blocked.append(blocked_item(vc, row, state, "本店商品码对应多个完整 VC"))
            del prepared[vc]
    return prepared


def plan_price(vcs, shops, state, manual, discount, club, keep_price=False, *,
               resolver=None, allow_discount_only=False, shop_rows=None):
    validate_price_intent(manual, keep_price, discount, club)
    plans = []
    for sid in shops:
        rows = shop_rows[sid] if shop_rows is not None else load_shop_rows(sid)
        plan, targets = shop_targets(sid, vcs, rows, state)
        plans.append(plan)
        items, blocked = plan["items"], plan["blocked_items"]
        prepared = prepare_cards(sid, targets, rows, state, blocked, resolver,
                                 price=keep_price and not allow_discount_only)
        for vc, row in prepared.items():
            nm_id = positive_id(row["nmId"])
            cur = first_price(row)
            if keep_price and allow_discount_only:
                price = None
            elif keep_price:
                price = bcs_keep_price(row)
                if price is None:
                    blocked.append(blocked_item(vc, row, state, "无法无损保持全部规格的单一原价"))
                    continue
            else:
                price = manual
                if cur is not None and price <= cur / 2:
                    blocked.append(blocked_item(vc, row, state, f"目标价 {price} 低于原价一半或等于一半（原价 {cur}）"))
                    continue
            items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""), "nmID": nm_id,
                          "price": price, "cur_price": cur, "discount": discount, "clubDiscount": club,
                          "keep_price": keep_price})
        if resolver:
            resolver.block_after_auth_failure(sid, items, blocked)
    report_plan_skips(plans, "改价")
    return plans


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
    if len({int(s[0]) for s in specs}) != len(specs):
        return "下架清库存计划包含重复规格"
    if len({int(s[1]) for s in specs}) != 1:
        return "下架清库存计划不能涉及多个仓库"
    return None



def _claims(owners: dict[int, set[str]], vc: str, row: dict) -> None:
    sizes = row.get("sizeList")
    if isinstance(sizes, list):
        for size in sizes:
            if isinstance(size, dict) and (spec := positive_id(size.get("chrtId"))) is not None:
                owners[spec].add(vc)


def _stock_plans(vcs, shops, state, amount, *, trash, resolver, shop_rows):
    plans = []
    for sid in shops:
        rows = shop_rows[sid] if shop_rows is not None else load_shop_rows(sid)
        plan, targets = shop_targets(sid, vcs, rows, state)
        plans.append(plan)
        plan["warehouses"] = []
        blocked = plan["blocked_items"]
        if not targets:
            continue
        wh = warehouse.resolve_warehouse(sid)
        if wh is None:
            blocked.extend(blocked_item(vc, row, state, "无法确认该店莫斯科仓库") for vc, row in targets.items())
            continue
        owners = defaultdict(set)
        for vc, row in rows.items():
            _claims(owners, vc, row)
        for index, row in enumerate(getattr(rows, "other_cards", []), 1):
            vc = row.get("vendorCode") or row.get("goodsCode")
            nm = positive_id(row.get("nmId"))
            if not (isinstance(vc, str) and vc in rows and nm is not None
                    and nm == positive_id(rows[vc].get("nmId"))):
                _claims(owners, f"非在架目标行 {index}（{vc or nm or '缺标识'}）", row)
        prepared_rows = {}
        for vc, row in targets.items():
            if resolver:
                prepared = resolver.ensure(sid, vc, row, nm=trash, specs=True)
                if prepared.reason:
                    blocked.append(blocked_item(vc, row, state, prepared.reason))
                    continue
                row = prepared.row
            _claims(owners, vc, row)
            prepared_rows[vc] = row

        items = plan["items"]
        for vc, row in prepared_rows.items():
            sizes = valid_sizes(row)
            if sizes is None:
                blocked.append(blocked_item(vc, row, state, "缺少完整且唯一的规格（sizeList/chrtId）"))
                continue
            ids = sorted(positive_id(size["chrtId"]) for size in sizes)
            shared = [(spec, sorted(owners[spec] - {vc})) for spec in ids if owners[spec] - {vc}]
            if shared:
                evidence = "；".join(f"{spec} 与 {', '.join(others)}" for spec, others in shared)
                blocked.append({**blocked_item(vc, row, state, f"规格归属冲突：{evidence}"),
                                "candidate_specs": ids})
                continue
            item = {"vc": vc, "cn": state.get(vc, {}).get("cn", ""), "nmID": positive_id(row.get("nmId"))}
            if trash:
                item["stock_specs"] = [(spec, wh) for spec in ids]
                if reason := trash_item_error(item):
                    blocked.append(blocked_item(vc, row, state, reason))
                    continue
                items.append(item)
            else:
                by_id = {positive_id(size["chrtId"]): size for size in sizes}
                for spec in ids:
                    current = stock_at(by_id[spec], wh)
                    items.append({**item, "chrtId": spec, "warehouseId": wh, "amount": amount,
                                  "cur_amt": current})
        if resolver:
            resolver.block_after_auth_failure(sid, items, blocked)
        if items and not trash:
            plan["warehouses"] = [{"warehouseId": wh, "stockItems": [
                {"chrtId": item["chrtId"], "amount": amount} for item in sorted(items, key=lambda it: it["chrtId"])]}]
    return plans


def plan_stock(vcs, shops, state, amount, *, resolver=None, shop_rows=None, report=True):
    plans = _stock_plans(vcs, shops, state, amount, trash=False, resolver=resolver, shop_rows=shop_rows)
    if report:
        report_plan_skips(plans, "设库存")
    return plans


def plan_trash(vcs, shops, state, *, resolver=None, shop_rows=None):
    plans = _stock_plans(vcs, shops, state, 0, trash=True, resolver=resolver, shop_rows=shop_rows)
    report_plan_skips(plans, "下架")
    return plans


def dry_run(plans: list[dict], action: str) -> None:
    print("\n===== 计划清单（预览，未提交）=====")
    for plan in plans:
        items = plan["items"]
        blocked, skipped = plan.get("blocked_items", []), plan.get("skipped_items", [])
        specs = f"，{len(items)} 个规格" if action == "stock" else ""
        print(f"店{plan['shopId']}：可提交 {len({it['vc'] for it in items})}{specs}，"
              f"阻止 {len(blocked)}，正常跳过 {len(skipped)}")
        for item in items:
            if action == "price":
                target = ", ".join(f"{field}={item[field]}" for field in ("price", "discount", "clubDiscount")
                                   if item.get(field) is not None)
                if item.get("keep_price"):
                    label = f"各规格原价 {item['price']}" if item["price"] is not None else "保留各规格原价"
                    target = f"{label}，{target}"
            elif action == "stock":
                current = item["cur_amt"] if item["cur_amt"] is not None else "未知"
                target = f"库存 {current} → {item['amount']}（规格 {item['chrtId']}，莫斯科仓库 {item['warehouseId']}）"
            else:
                target = f"清零 {len(item['stock_specs'])} 个规格后移回收站（nmID={item['nmID']}）"
            print(f"  {item['vc']} | {item['cn']} | {target}")
        for item in blocked:
            print(f"  [已阻止] {item['vc']} | {item['reason']}")

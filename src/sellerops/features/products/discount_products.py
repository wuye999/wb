"""定向折扣只读取本店商品标识和条件判断所需的当前折扣。"""
from sellerops.domain.snapshot_values import positive_id
from sellerops.features.products.plans import blocked_item, prepare_cards, shop_targets
from sellerops.features.products.product_data import ProductDataResolver, valid_discount


def targeted_discount_plan(sid: int, vcs: list[str], rows: dict | None, state: dict,
                           args, resolver: ProductDataResolver) -> dict:
    plan, targets = shop_targets(sid, vcs, rows, state)
    blocked = plan["blocked_items"]
    prepared = prepare_cards(sid, targets, rows, state, blocked, resolver)
    if sid in resolver.auth_failures:
        blocked.extend(blocked_item(vc, row, state, resolver.auth_failures[sid]) for vc, row in prepared.items())
        return plan

    threshold = args.threshold
    below = getattr(args, "below", -1)
    conditional = (threshold is not None and threshold >= 0) or below >= 0
    values, failures = resolver.current_discounts(sid, prepared) if conditional else ({}, {})
    for vc, row in prepared.items():
        if vc in failures:
            blocked.append(blocked_item(vc, row, state, failures[vc]))
            continue
        old = values.get(vc) if conditional else row.get("discount")
        item = {"vc": vc, "cn": state.get(vc, {}).get("cn", ""), "nmID": positive_id(row["nmId"]),
                "old_discount": old if valid_discount(old) else None, "discount": args.target,
                "currencyIsoCode": row.get("currencyIsoCode") or "CNY"}
        if conditional and not ((threshold is not None and threshold >= 0 and old > threshold)
                                or (below >= 0 and old < below)):
            plan["skipped_items"].append({**item, "reason": "平台当前折扣不满足阈值"})
        elif args.limit and len(plan["items"]) >= args.limit:
            plan["skipped_items"].append({**item, "reason": "超出每店数量限制"})
        else:
            plan["items"].append(item)
    return plan

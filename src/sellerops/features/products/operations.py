"""BCS 改价、设库存和两种下架通道的计划与提交。"""
import time

from sellerops.core import utils as common
from sellerops.features.catalog.selection import load_state, resolve_filters, selected_shops
from sellerops.features.products.execution import apply_trash, submit_bcs_prices, submit_bcs_stock, SHOP_SLEEP
from sellerops.features.products.price_review import review_submitted_prices
from sellerops.features.products.results import ProductResult
from sellerops.features.products.plans import RED, RESET, dry_run, plan_price, plan_stock, plan_trash
from sellerops.features.products.product_data import ProductDataResolver


def confirm_irreversible(action, amount, yes):
    danger = action == "trash" or (action == "stock" and amount == 0)
    if not danger:
        return True
    if yes:
        return True
    tip = "移至回收站" if action == "trash" else f"库存归零(amount={amount})"
    print(f"\n{RED}⚠ 即将执行不可逆操作：{tip}！{RESET}")
    try:
        ans = input("输入 y 确认执行，其他任意键取消: ").strip().lower()
        return ans == "y"
    except EOFError:
        return False


def run(action: str, args, *, channel: str) -> int:
    common.validate_batch_options(chunk=getattr(args, "chunk", None), interval=getattr(args, "interval", None))
    if action == "trash":
        return _trash(args, channel)
    return ProductResult(action, channel, args).run(_bcs, args)


def _bcs(args, result: ProductResult) -> None:
    shops = result.shops = selected_shops(args)
    state, shop_rows, book = load_state(shops, all_products=getattr(args, "all_products", False),
                                       fresh=(result.action == 'price' and result.apply and getattr(args, 'price', None) is None
                                              and not getattr(args, 'keep_price', False)
                                              and (getattr(args, 'from_price_book', False)
                                                   or getattr(args, 'discount', None) is None)))
    vcs, desc = resolve_filters(args, state, book)
    print(f"目标：{desc}，通道 BCS")
    with ProductDataResolver() as resolver:
        if result.action == "price":
            plans = plan_price(vcs, shops, state, getattr(args, "price", None), getattr(args, "discount", None),
                               getattr(args, "club_discount", None), keep_price=getattr(args, "keep_price", False),
                               resolver=resolver, shop_rows=shop_rows,
                               from_price_book=getattr(args, "from_price_book", False))
        else:
            plans = plan_stock(vcs, shops, state, args.amount, resolver=resolver, shop_rows=shop_rows)
    result.add_plans(plans)
    if not result.apply:
        dry_run(plans, result.action)
        return
    if not any(plan["items"] for plan in plans):
        return
    if not confirm_irreversible(result.action, getattr(args, "amount", None), getattr(args, "yes", False)):
        result.cancelled = True
        print("已取消，未执行任何操作")
        return
    for plan in plans:
        if not plan["items"]:
            continue
        sid = plan["shopId"]
        if result.action == "price":
            accepted = submit_bcs_prices(sid, plan["items"], result)
            if result.review_requested:
                review_submitted_prices(sid, accepted, result)
        else:
            submit_bcs_stock(plan, result)
        time.sleep(SHOP_SLEEP)
    common.print_write_hint()


def _trash(args, channel: str) -> int:
    shops = selected_shops(args)
    state, shop_rows, book = load_state(shops, all_products=getattr(args, "all_products", False))
    vcs, desc = resolve_filters(args, state, book)
    print(f"目标：{desc}，通过 {channel.upper()} 移回收站（先清库存）")
    with ProductDataResolver() as resolver:
        plans = plan_trash(vcs, shops, state, resolver=resolver, shop_rows=shop_rows)
    failed = any(plan.get("range_error") or plan["blocked_items"] for plan in plans)
    if not getattr(args, "apply", False):
        dry_run(plans, "trash")
        return int(failed)
    if not any(plan["items"] for plan in plans):
        return int(failed)
    if not confirm_irreversible("trash", None, getattr(args, "yes", False)):
        return int(failed)
    _, failures = apply_trash(plans, channel=channel, chunk=getattr(args, "chunk", None),
                             interval=getattr(args, "interval", None))
    common.print_write_hint()
    return int(failed or failures > 0)

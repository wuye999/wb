"""BCS 改价、设库存和两种下架通道的计划与提交。"""
import sys

from sellerops.core import utils as common
from sellerops.features.catalog.selection import load_state, resolve_filters, selected_shops
from sellerops.features.products.execution import run_apply
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


def run(action, args, *, channel):
    common.validate_batch_options(chunk=getattr(args, "chunk", None),
                                  interval=getattr(args, "interval", None))
    if action == "trash":
        print(f"通道: {'WB 原生 moveNmsToTrash' if channel == 'wb' else 'BCS 移回收站'}"
              f"（先清库存 → 再移回收站）")

    for d in ("discount", "club_discount"):
        v = getattr(args, d, None)
        if v is not None and not (0 <= v <= 100):
            print(f"[错误] {d} 需在 0-100 之间")
            sys.exit(1)

    shops = selected_shops(args)
    state, shop_rows, boss = load_state(shops, all_products=getattr(args, "all_products", False))
    vcs, desc = resolve_filters(args, state, boss)
    if not vcs:
        print("[提示] 筛选结果为空，无操作")
        return
    print(f"目标: {desc}（{len(vcs)} 个 vendorCode）")

    with ProductDataResolver() as resolver:
        if action == "price":
            manual = getattr(args, "price", None)
            plans = plan_price(vcs, shops, state, boss, manual, getattr(args, "discount", None),
                               getattr(args, "club_discount", None),
                               keep_price=getattr(args, "keep_price", False), resolver=resolver, shop_rows=shop_rows)
        elif action == "stock":
            plans = plan_stock(vcs, shops, state, getattr(args, "amount", 0), resolver=resolver, shop_rows=shop_rows)
        else:
            plans = plan_trash(vcs, shops, state, resolver=resolver, shop_rows=shop_rows)

    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法操作（上方 [跳过] / [跳过汇总] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / sizeList 无 chrtId。")
        return

    if not getattr(args, "apply", False):
        dry_run(plans, action, getattr(args, "amount", None))
        return 1 if any(p.get("blocked_items") for p in plans) else 0

    if not confirm_irreversible(action, getattr(args, "amount", 0), getattr(args, "yes", False)):
        print("已取消，未执行任何操作")
        return

    ok, fail = run_apply(plans, action, auto_review=getattr(args, "auto_review", False),
              channel=channel,
              chunk=getattr(args, "chunk", None),
              interval=getattr(args, "interval", None))
    # 改价/库存/下架 --apply 提交后：默认不自动同步/写后验证/合并（WB/BCS 异步回填，当场验证不准）。
    if action in ("price", "stock", "trash"):
        common.print_write_hint()
    return 1 if fail else 0



"""
sellerops 一键操作框架：按映射表批量改价 / 改库存 / 下架（原 ops.py）

安全机制：默认 dry-run；--apply 执行；trash / 库存归零 需 --yes 确认。
数据前提：操作前先 sellerops fetch（从各店 JSON 定位 nmId/chrtId/warehouseId）。
本模块作为业务门面，核心计划逻辑在 ops_plan，批处理执行在 ops_executor。
"""


import sys
from sellerops.core import utils as common

from sellerops.persistence.snapshots import ProductSnapshotRepository

from sellerops.features.listings.plans import RED


from sellerops.features.listings.plans import RESET

from sellerops.features.listings.plans import load_state

from sellerops.features.listings.plans import resolve_filters


from sellerops.features.listings.plans import plan_price


from sellerops.features.listings.plans import plan_stock
from sellerops.features.listings.plans import plan_trash
from sellerops.features.listings.plans import dry_run


from sellerops.features.listings.execution import run_apply
from sellerops.features.listings.product_data import ProductDataResolver

def get_shops():
    """扫描 data/products/ 下 shop{id}_products_all.json → [店id]"""
    return ProductSnapshotRepository.shop_ids_from_disk()


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


def _default_channel(action):
    """默认执行通道（纯函数，便于单测）。

    trash 默认 WB 原生 moveNmsToTrash（2026-10-29 起，替代 BCS clean/removeToTrash）；
    price / stock 仍默认 BCS —— 它们的 WB 原生通道分别是 price_wb / stock_wb，
    不走本模块的 run()。
    """
    return "wb" if action == "trash" else "bcs"


def run(action, args, channel=None):
    common.validate_batch_options(chunk=getattr(args, "chunk", None),
                                  interval=getattr(args, "interval", None))
    if channel is None:
        channel = _default_channel(action)
    if action == "trash":
        print(f"通道: {'WB 原生 moveNmsToTrash' if channel == 'wb' else 'BCS clean/removeToTrash'}"
              f"（先清库存 → 再移回收站）")

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
        print("[错误] 未找到任何店铺数据文件，请先运行 sellerops fetch")
        sys.exit(1)
    shops = [int(s) for s in args.shops.split(",")] if getattr(args, "shops", None) else all_shops
    missing = [s for s in shops if s not in all_shops]
    if missing:
        print(f"[警告] 以下店铺无数据文件（未 fetch）：{missing}，将记录为计划失败")

    state, _, boss = load_state()

    with ProductDataResolver() as resolver:
        if action == "price":
            manual = getattr(args, "price", None)
            plans = plan_price(vcs, shops, state, boss, manual, getattr(args, "discount", None),
                               getattr(args, "club_discount", None),
                               keep_price=getattr(args, "keep_price", False), resolver=resolver)
        elif action == "stock":
            plans = plan_stock(vcs, shops, state, getattr(args, "amount", 0), resolver=resolver)
        else:
            plans = plan_trash(vcs, shops, state, resolver=resolver)

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


def add_ops_args(p, *, with_price=False, with_stock=False):
    """[薄转发] 参数定义唯一实现已下沉到 framework.cli_args（argparse-only，cli 与 service 共用）。"""
    from sellerops.cli.arguments import add_ops_args as _impl
    return _impl(p, with_price=with_price, with_stock=with_stock)

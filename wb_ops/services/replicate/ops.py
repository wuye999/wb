# -*- coding: utf-8 -*-
"""
wb_ops 一键操作框架：按映射表批量改价 / 改库存 / 下架（原 ops.py）

安全机制：默认 dry-run；--apply 执行；trash / 库存归零 需 --yes 确认。
数据前提：操作前先 wb.py fetch（从各店 JSON 定位 nmId/chrtId/warehouseId）。
本模块作为业务门面，核心计划逻辑在 ops_plan，批处理执行在 ops_executor。
"""
import sys
from wb_ops.adapters import bcs_client as bcs
from wb_ops import common
from wb_ops import config
from wb_ops.services.catalog import products

# 重新导出常量与核心函数，保持 100% 向后兼容
from .ops_plan import (
    RED, GREEN, YELLOW, RESET,
    PRICE_HALF_LIMIT_NOTE,
    load_state,
    load_shop_rows,
    resolve_filters,
    target_price,
    SkipTracker,
    plan_price,
    price_limit_violations,
    price_review_items,
    plan_stock,
    plan_trash,
    dry_run,
)
from .ops_executor import (
    PRICE_CHUNK,
    STOCK_CHUNK,
    BATCH_SLEEP,
    SHOP_SLEEP,
    RESULT_CSV,
    _auto_review_shop,
    _normalize_csv_encoding,
    _endpoint,
    _post,
    run_apply,
)

_shop_warehouses_cache = {}


def get_shops():
    """扫描 data/products/ 下 shop{id}_products_all.json → [店id]"""
    return products.shop_ids_from_disk()


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
    shops = [int(s) for s in args.shops.split(",")] if getattr(args, "shops", None) else all_shops
    missing = [s for s in shops if s not in all_shops]
    if missing:
        print(f"[警告] 以下店铺无数据文件（未 fetch）：{missing}，将跳过")
        shops = [s for s in shops if s in all_shops]

    state, _, boss = load_state()

    if action == "price":
        manual = getattr(args, "price", None)
        plans = plan_price(vcs, shops, state, boss, manual, getattr(args, "discount", None),
                           getattr(args, "club_discount", None),
                           keep_price=getattr(args, "keep_price", False))
    elif action == "stock":
        plans = plan_stock(vcs, shops, state, getattr(args, "amount", 0))
    else:
        plans = plan_trash(vcs, shops, state)

    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法操作（上方 [跳过] / [跳过汇总] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / sizeList 无 chrtId。")
        return

    if not getattr(args, "apply", False):
        dry_run(plans, action, getattr(args, "amount", None))
        return

    if not confirm_irreversible(action, getattr(args, "amount", 0), getattr(args, "yes", False)):
        print("已取消，未执行任何操作")
        return

    run_apply(plans, action, auto_review=getattr(args, "auto_review", False))
    # 改价/库存/下架 --apply 提交后：默认不自动同步/写后验证/合并（WB/BCS 异步回填，当场验证不准）。
    # 只有加 --sync 才同步在架商品并增量合并映射表；否则仅打印提示。
    if action in ("price", "stock", "trash"):
        if getattr(args, "sync", False):
            from wb_ops.services.catalog import mapping_sync
            mapping_sync.post_write_merge()  # fetch=True：全店同步+拉取+merge
        else:
            common.print_write_hint()


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
    p.add_argument("--sync", action="store_true",
                   help="执行后自动同步在架商品并合并映射表（默认不自动同步/不写后验证，仅打印提示）")
    if with_price:
        p.add_argument("--price", type=int, help="目标价（默认 floor(商品价格表双倍售价)）")
        p.add_argument("--discount", type=int, help="折扣 0-100（不传=不改）")
        p.add_argument("--club-discount", type=int, help="club折扣 0-100（不传=不改）")
        p.add_argument("--keep-price", action="store_true", help="价格保持当前值（只改折扣/俱乐部折扣）")
        p.add_argument("--auto-review", action="store_true",
                       help="改价后自动「应用新价格」（降价 30-49.9%% 进审查时，精确匹配刚改价商品）")
    if with_stock:
        p.add_argument("--amount", type=int, default=0, help="目标库存（默认 0）")

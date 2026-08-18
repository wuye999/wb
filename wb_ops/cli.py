# -*- coding: utf-8 -*-
"""
wb_ops 统一 CLI 入口（聚合「检查价格」+「促销折扣」全部功能）

用法：python wb.py <子命令>（或 python -m wb_ops <子命令>）
子命令见 docs/CLI.md；每个子命令的实现在对应业务模块中，本文件只做解析与分发。
"""
import argparse
import sys

from . import common

from . import bcs
from . import clean
from . import config
from . import cookies
from . import daily
from . import discount
from . import mapping
from . import mapping_check
from . import mapping_sync
from . import ops
from . import products
from . import promo
from . import schedule
def build_parser():
    ap = argparse.ArgumentParser(prog="wb", description="Wildberries/BCS 卖家自动化统一入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("shops", help="账号店铺列表")

    p = sub.add_parser("fetch", help="拉取店铺商品数据（自动先同步 WB）")
    p.add_argument("--shop-id", type=int, default=None, help="店铺ID（默认全部）")
    p.add_argument("--no-sync", action="store_true", help="跳过 BCS 同步，直接拉取上次数据")

    p = sub.add_parser("mapping", help="生成统一核对工作台（5 店并集，一页两区）")
    p.add_argument("--legacy", action="store_true", help="旧模式：仅主店候选")

    p = sub.add_parser("mapping-import", help="导入核对结果 → 生成映射表（旧格式）")
    p.add_argument("file", help="核对结果 JSON")

    p = sub.add_parser("mapping-check", help="映射表核查工作台（带图，核对匹配是否有误）")
    p.add_argument("--tol", type=int, default=5, help="价格偏差阈值（元，默认 5）")

    sub.add_parser("review", help="多店铺待审核工作台")

    p = sub.add_parser("merge", help="增量合并审核 → 映射表（file 可选）")
    p.add_argument("file", nargs="?", default=None, help="本次审核结果 JSON（可选）")

    p = sub.add_parser("price", help="改价/改折扣（dry-run 默认，--apply 执行）")
    ops.add_ops_args(p, with_price=True)

    p = sub.add_parser("stock", help="改库存")
    ops.add_ops_args(p, with_stock=True)

    p = sub.add_parser("trash", help="下架（移回收站，不可逆）")
    ops.add_ops_args(p)

    p = sub.add_parser("promo-apply", help="促销报名（cookie 会话，applyAll）")
    p.add_argument("--apply", action="store_true", help="真正报名（默认 dry-run 预览）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--days", type=int, default=60, help="查询结束=N天后")
    p.add_argument("--days-back", type=int, default=90, help="查询起始=N天前")
    p.add_argument("--sleep", type=float, default=1.0, help="活动间请求间隔秒")

    p = sub.add_parser("discount", help="折扣改价（各店 >阈值 → 目标折扣）")
    p.add_argument("--apply", action="store_true", help="真正提交（默认 dry-run）")
    p.add_argument("--threshold", type=int, default=config.DISCOUNT_THRESHOLD_DEF, help="阈值（处理 > 该值）")
    p.add_argument("--target", type=int, default=config.DISCOUNT_TARGET_DEF, help="目标折扣")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    p.add_argument("--no-sync", action="store_true", help="跳过同步（缓存滞后会漏查！）")
    p.add_argument("--sync", action="store_true", help="提交后同步复核")

    p = sub.add_parser("clean", help="清理草稿箱/回收站")
    p.add_argument("--target", required=True, choices=["basket", "draft", "all"],
                   help="basket=回收站 / draft=草稿箱 / all=先草稿后回收站")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条")
    p.add_argument("--no-sync", action="store_true", help="跳过列表前同步")

    p = sub.add_parser("cookies-update", help="从抓包 md 刷新凭证")
    p.add_argument("md_file", help="含 fetch 块的 md 文件")

    p = sub.add_parser("daily", help="每日任务（morning=报名+改价 / check=只改价）")
    p.add_argument("mode", choices=["morning", "check"])
    p.add_argument("extra", nargs=argparse.REMAINDER, help="透传给子步骤（如 --shops 5272）")

    p = sub.add_parser("schedule", help="创建/删除 Windows 计划任务")
    p.add_argument("--remove", action="store_true", help="删除全部任务")

    return ap


def dispatch(args):
    cmd = args.cmd
    if cmd == "shops":
        for s in bcs.fetch_shop_list():
            print(f"{s['id']} | {s['name']}")
        return 0
    if cmd == "fetch":
        if args.shop_id:
            out = config.shop_json_path(args.shop_id)
            products.fetch_shop(args.shop_id, out, no_sync=args.no_sync)
        else:
            products.fetch_all(no_sync=args.no_sync)
        return 0
    if cmd == "mapping":
        mapping.run_mapping(legacy=args.legacy)
        return 0
    if cmd == "mapping-import":
        mapping.import_mapping(args.file)
        return 0
    if cmd == "mapping-check":
        mapping_check.run(tol=args.tol)
        return 0
    if cmd == "review":
        mapping_sync.run_review()
        return 0
    if cmd == "merge":
        mapping_sync.run_merge(args.file)
        return 0
    if cmd in ("price", "stock", "trash"):
        ops.run(cmd, args)
        return 0
    if cmd == "promo-apply":
        return promo.run(args)
    if cmd == "discount":
        return discount.run(args)
    if cmd == "clean":
        return clean.run(args)
    if cmd == "cookies-update":
        return cookies.run(args.md_file)
    if cmd == "daily":
        return daily.run(args.mode, args.extra)
    if cmd == "schedule":
        return schedule.run(args)
    return 0


def main(argv=None):
    common.ensure_utf8_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        code = dispatch(args)
    except KeyboardInterrupt:
        print("\n已中断")
        code = 130
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[错误] {e}", file=sys.stderr)
        code = 1
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()

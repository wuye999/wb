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
from . import banned
from . import clean
from . import config
from . import cookies
from . import daily
from . import discount
from . import import_shelve
from . import mapping
from . import mapping_check
from . import mapping_sync
from . import mismatch_check
from . import ops
from . import orders
from . import price_review
from . import products
from . import promo
from . import questions
from . import questions_watch
from . import ai_reply_test
from . import replicate
from . import remote_wh
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

    p = sub.add_parser("mismatch-check", help="货不对板筛查工作台（看图勾选，导出 vc 下架）")
    p.add_argument("--cn", default="", help="只渲染指定中文名（精确匹配，如『牙膏-紫色』）")

    sub.add_parser("review", help="多店铺待审核工作台")

    p = sub.add_parser("merge", help="增量合并审核 → 映射表（file 可选）")
    p.add_argument("file", nargs="?", default=None, help="本次审核结果 JSON（可选）")

    p = sub.add_parser("price", help="改价/改折扣（dry-run 默认，--apply 执行）")
    ops.add_ops_args(p, with_price=True)

    p = sub.add_parser("stock", help="改库存")
    ops.add_ops_args(p, with_stock=True)

    p = sub.add_parser("trash", help="下架（移回收站，不可逆）")
    ops.add_ops_args(p)

    p = sub.add_parser("replicate", help="跨店复制上架：把部分覆盖的商品上架到缺失店铺（dry-run 默认）")
    p.add_argument("--vc", default="", help="指定 vendorCode（逗号分隔）")
    p.add_argument("--prefix", default="", help="前缀码筛选（4 位大写）")
    p.add_argument("--name", default="", help="映射表中文名包含匹配")
    p.add_argument("--shops", default="", help="限定目标店铺 id 逗号分隔（默认全部缺失店）")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个 vc（0=不限）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--no-verify", action="store_true", help="跳过执行后 fetch 复核")
    p.add_argument("--no-sync", action="store_true", help="跳过启动时自动同步（默认自动 fetch 全部店铺，约 2 分钟）")
    p.add_argument("--interval", type=float, default=1.0, help="上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")

    p = sub.add_parser("import-shelve", help="他人映射表导入上架：他人有我方无的商品（按 WB原始nmId 匹配）上架到我的店铺")
    p.add_argument("xlsx", help="他人映射表 xlsx 路径（同项目「映射总表」格式）")
    p.add_argument("--cn", default="", help="他人表中文名包含过滤")
    p.add_argument("--shops", default="", help="限定目标店铺 id 逗号分隔（默认全部店铺）")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个商品（0=不限）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--no-verify", action="store_true", help="跳过执行后 fetch 复核")
    p.add_argument("--no-sync", action="store_true", help="跳过启动时自动同步（默认自动 fetch 全部店铺，约 2 分钟）")
    p.add_argument("--interval", type=float, default=1.0, help="上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")

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

    p = sub.add_parser("banned", help="查询并删除被阻止的商品（dry-run 默认，--apply 移到回收站）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    p.add_argument("--yes", action="store_true", help="跳过不可逆确认（移到回收站）")
    p.add_argument("--no-verify", action="store_true", help="跳过执行后验证")

    p = sub.add_parser("clean", help="清理草稿箱/回收站")
    p.add_argument("--target", required=True, choices=["basket", "draft", "all"],
                   help="basket=回收站 / draft=草稿箱 / all=先草稿后回收站")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条")
    p.add_argument("--no-sync", action="store_true", help="跳过列表前同步")

    p = sub.add_parser("price-review", help="价格审核：查看并应用新价格（降价 30-49.9%% 进审查的商品）")
    p.add_argument("--apply", action="store_true", help="真正应用新价格（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多审核 N 个（0=全部）")

    p = sub.add_parser("orders", help="订单查询（自动同步 + 查询指定日期区间）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    p.add_argument("--days", type=int, default=0, help="查询最近 N 天（--begin/--end 未指定时，默认 1 天）")
    p.add_argument("--no-sync", action="store_true", help="跳过订单同步，直接查缓存")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部）")
    p.add_argument("--page-size", type=int, default=50, help="分页每页条数")

    p = sub.add_parser("questions", help="买家未处理提问查询与回复")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--reply", default="", help="回复内容（配合 --question-id 或 --reply-all）")
    p.add_argument("--question-id", default="", help="回复指定提问 ID")
    p.add_argument("--reply-all", action="store_true", help="回复本店全部未处理提问（需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认回复全部（公开发言）")
    p.add_argument("--no-detail", action="store_true", help="跳过 WB 商品详情拉取（只显示本地中文名，速度快）")

    p = sub.add_parser("questions-watch", help="买家提问实时监听（后台 AI 模式：常驻轮询+DeepSeek 自动回复）")
    p.add_argument("--interval", type=int, default=0, help="轮询间隔秒（默认 90，或 credentials 的 ai.watch_interval）")
    p.add_argument("--apply", action="store_true", help="真正提交回复（默认 dry-run 只打草稿）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--once", action="store_true", help="只跑一轮就退出（测试用）")

    p = sub.add_parser("ai-test", help="离线用 data/ai_test_qa.json 对照测试 AI 客服回复（不联网）")
    p.add_argument("--qa", default=config.AI_TEST_QA, help="测试数据集 json（默认 data/ai_test_qa.json）")

    p = sub.add_parser("cookies-update", help="从抓包 md 刷新凭证")
    p.add_argument("md_file", help="含 fetch 块的 md 文件")

    p = sub.add_parser("daily", help="每日任务（morning=报名+改价 / check=只改价）")
    p.add_argument("mode", choices=["morning", "check"])
    p.add_argument("extra", nargs=argparse.REMAINDER, help="透传给子步骤（如 --shops 5272）")

    p = sub.add_parser("schedule", help="创建/删除 Windows 计划任务")
    p.add_argument("--remove", action="store_true", help="删除全部任务")

    remote_wh.build_parser(sub)

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
    if cmd == "mismatch-check":
        mismatch_check.run(cn=args.cn)
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
    if cmd == "replicate":
        return replicate.run(args)
    if cmd == "import-shelve":
        return import_shelve.run(args)
    if cmd == "promo-apply":
        return promo.run(args)
    if cmd == "discount":
        return discount.run(args)
    if cmd == "banned":
        return banned.run(args)
    if cmd == "clean":
        return clean.run(args)
    if cmd == "price-review":
        return price_review.run(args)
    if cmd == "orders":
        return orders.run(args)
    if cmd == "questions":
        return questions.run(args)
    if cmd == "questions-watch":
        return questions_watch.run(args)
    if cmd == "ai-test":
        return ai_reply_test.run(args)
    if cmd == "cookies-update":
        return cookies.run(args.md_file)
    if cmd == "daily":
        return daily.run(args.mode, args.extra)
    if cmd == "schedule":
        return schedule.run(args)
    if cmd == "remote-wh":
        return remote_wh.run(args)
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

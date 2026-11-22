"""命令定义：product。"""
import argparse
from sellerops.cli.binding import bind
from sellerops.cli.arguments import add_ops_args
from sellerops.cli.arguments import add_discount_args
from sellerops.cli.arguments import add_shop_scope
from sellerops.cli.arguments import add_product_scope


def register(sub: argparse._SubParsersAction, *, planned_only: bool = False) -> None:
    p = sub.add_parser('price', help="改价或按显式价格意图设折扣（默认 WB 后台，默认预览）",
                       description="按表改价可直接使用本命令。只设折扣用 product discount --target D。"
                                   "按表同时改价和设折扣用 --from-price-book --discount D。")
    add_ops_args(p, with_price=True)
    p.add_argument("--chunk", type=int, default=100, help="每批提交条数（≤300，对齐 discount 实践，默认 100）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")
    p.add_argument('--channel', choices=['wb', 'bcs'], default='wb', help='WB 支持设置批次，BCS 使用固定批次；俱乐部折扣仅 BCS 支持')
    bind(p, 'sellerops.cli.product', 'run_price')

    p = sub.add_parser('stock', help="改库存（默认 WB 原生在线接口；dry-run 默认，归零须 --yes）")
    add_ops_args(p, with_stock=True)
    p.add_argument("--chunk", type=int, default=500, help="每批提交条数（≤1000，探针实测 1000 可行，默认 500）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")
    p.add_argument("--max-pages", type=int, default=200, help="WB 游标分页安全上限（--resolve live 时使用）")
    p.add_argument("--resolve", choices=["snapshot", "live"], default="snapshot",
                       help="snapshot=本地完整规格（默认，缺标识时官方API定向补齐）；live=另读莫斯科当前库存，缺库存记录不减少规格")
    p.add_argument('--channel', choices=['wb', 'bcs'], default='wb', help='WB 支持实时库存；BCS 仅支持快照和固定批次')
    bind(p, 'sellerops.cli.product', 'run_stock')

    p = sub.add_parser('trash', help="下架（默认 WB 原生 moveNmsToTrash 移回收站；先清库存；不可逆）")
    add_ops_args(p)
    p.add_argument("--chunk", type=int, default=300, help="每批移回收站条数（≤300，默认 300）")
    p.add_argument("--interval", type=float, default=0.3, help="批间间隔秒")
    p.add_argument('--channel', choices=['wb', 'bcs'], default='wb', help='提交平台，默认 WB 后台')
    bind(p, 'sellerops.cli.product', 'run_trash')

    if not planned_only:
        p = sub.add_parser('dimensions', help="批量修改尺寸（按价格表或 --dims 自定义，dry-run 默认，--apply 执行）")
        add_product_scope(p)
        p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
        p.add_argument("--dims", default="", help="自定义尺寸 '长*宽*高/毛重'（例: 8*14*26/0.3）")
        p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")

        bind(p, 'sellerops.features.products.dimension', 'run')

        p = sub.add_parser('check-dimensions', help="查询 WB 包装尺寸/重量偏差待验证商品（只读）")
        p.add_argument("--type", choices=["dims", "weight", "all"], default="dims",
                           help="dims=尺寸偏差 / weight=重量偏差 / all=两者合并去重（默认 dims）")
        names = p.add_mutually_exclusive_group()
        names.add_argument("--name", default="", help="完整中文商品名")
        names.add_argument("--name-contains", help="中文商品名包含指定文字")
        p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
        p.add_argument("--limit", type=int, default=0, help="每店最多查询 N 条（0=不限）")
        bind(p, 'sellerops.features.products.dims_check', 'run')

        p = sub.add_parser('banned', help="查询并删除被阻止的商品（dry-run 默认，--apply 移到回收站）")
        p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
        add_shop_scope(p)
        p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
        p.add_argument("--yes", action="store_true", help="跳过不可逆确认（移到回收站）")

        bind(p, 'sellerops.features.products.banned', 'run')

        p = sub.add_parser('clean', help="清理草稿箱/回收站")
        p.add_argument("--target", required=True, choices=["basket", "draft", "all"],
                           help="basket=回收站 / draft=草稿箱 / all=先草稿后回收站")
        p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
        add_shop_scope(p)
        p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条，草稿和回收站共用总额，0 为不限")

        p.add_argument("--yes", action="store_true", help="确认永久删除所列商品")
        bind(p, 'sellerops.features.products.clean', 'run')

    p = sub.add_parser('discount',
                           help="设目标折扣，--threshold 只修正超过阈值的商品。BCS 仅支持 --all-products")
    add_discount_args(p)
    bind(p, 'sellerops.cli.product', 'run_discount')

    p = sub.add_parser('review-price', help="价格审核：查看并应用新价格（降价 30-49.9%% 进审查的商品）")
    p.add_argument("--apply", action="store_true", help="真正应用新价格（默认 dry-run）")
    add_shop_scope(p)
    p.add_argument("--limit", type=int, default=0, help="每店最多审核 N 个（0=全部）")
    bind(p, 'sellerops.features.products.price_review', 'run')


def run_price(args):
    if args.price is not None and args.price <= 0:
        raise ValueError("目标价必须为正数")
    discounts = (args.discount, args.club_discount)
    if any(value is not None and not 0 <= value <= 100 for value in discounts):
        raise ValueError("折扣须在 0 至 100 之间")
    if args.club_discount is not None and args.channel == "wb":
        raise ValueError("俱乐部折扣需要显式选择 BCS 通道")
    has_discount = any(value is not None for value in discounts)
    if ((args.keep_price and not has_discount)
            or (has_discount and not (args.keep_price or args.from_price_book or args.price is not None))):
        raise ValueError("只改折扣用 product discount --target D 或 --keep-price --discount D，"
                         "按表同时改价用 --from-price-book --discount D。"
                         "BCS 俱乐部折扣同样须指定 --price、--from-price-book 或 --keep-price")
    if args.auto_review and args.keep_price:
        raise ValueError("自动审核需要提交新价格")
    if args.channel == "bcs":
        from sellerops.features.products.operations import run
        if args.chunk != 100 or args.interval != 0.3:
            raise ValueError("BCS 改价使用固定批次与间隔")
        return run("price", args, channel="bcs")
    from sellerops.features.products.prices import run
    return run(args)


def run_stock(args):
    if args.amount < 0:
        raise ValueError("库存数量必须大于等于零")
    if args.channel == "bcs":
        from sellerops.features.products.operations import run
        if args.resolve != "snapshot" or args.max_pages != 200 or args.chunk != 500 or args.interval != 0.3:
            raise ValueError("BCS 库存通道仅使用快照和固定批次")
        return run("stock", args, channel="bcs")
    from sellerops.features.products.stocks import run
    return run(args)


def run_trash(args):
    from sellerops.features.products.operations import run
    return run("trash", args, channel=args.channel)


def run_discount(args):
    if not 0 <= args.target <= 100 or args.limit < 0 or (args.chunk is not None and not 1 <= args.chunk <= 300):
        raise ValueError("请核对目标折扣、数量和批次大小")
    if (args.threshold is not None and not 0 <= args.threshold <= 100) or not -1 <= args.below <= 100:
        raise ValueError("折扣阈值须在 0 至 100 之间")
    if args.channel == "bcs":
        from sellerops.features.products.discount_bcs import run
        if not args.all_products or args.below >= 0 or args.chunk is not None:
            raise ValueError("BCS 折扣仅支持 --all-products 和 --threshold，不支持单品筛选或自定义批次")
        if args.threshold is None:
            args.threshold = -1
        return run(args)
    from sellerops.features.products.discounts import run_cli
    return run_cli(args)

"""pricing 命令定义。"""
import argparse
from sellerops.settings import paths as config
from sellerops.cli.binding import bind
from sellerops.cli.arguments import _add_discount_wb_args


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("promo-apply", help="促销报名（cookie 会话，applyAll）")
    p.add_argument("--apply", action="store_true", help="真正报名（默认 dry-run 预览）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--days", type=int, default=60, help="查询结束=N天后")
    p.add_argument("--days-back", type=int, default=90, help="查询起始=N天前")
    p.add_argument("--sleep", type=float, default=1.0, help="活动间请求间隔秒")
    bind(p, 'sellerops.features.pricing.promo', 'run')

    p = sub.add_parser("promo-goods", help="查询 WB 广告推广中被推广的商品（中文名/供应商代码/WB商品码，只读）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）")
    p.add_argument("--status", default="", help="活动状态ID逗号分隔（默认 4,9,11 = 后台默认视图）")
    p.add_argument("--page-size", type=int, default=100, help="活动列表分页大小（默认 100）")
    p.add_argument("--max-pages", type=int, default=50, help="活动列表最多翻页数（安全上限，默认 50）")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取 N 个活动（0=不限，翻页到底）")
    p.add_argument("--no-cn", action="store_true", help="不解析商品中文名（供应商代码仍解析）")
    bind(p, 'sellerops.features.pricing.adverts', 'run')

    p = sub.add_parser("promo-gap",
                           help="只读：推广 × 销量双向错配审计（gap=该推没推 / waste=在推但没销量该关）")
    p.add_argument("--mode", choices=["gap", "waste", "both"], default="gap",
                       help="审计方向：gap=该推没推(默认) / waste=该关的(在推广但本店单数<阈值) / both=两个都出")
    p.add_argument("--shops", default="", help="限定店铺，逗号分隔，支持店铺ID或短名（如 9352 或 袁州1；默认全部已填 cookie 店铺）")
    p.add_argument("--min", type=int, default=4, help="单数阈值（默认 4）：gap=单数≥N；waste=单数<N")
    p.add_argument("--days", type=int, default=7, help="近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）")
    p.add_argument("--date", default="", help="单天 YYYY-MM-DD（等价 --begin=--end）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（单独给按单天处理）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）")
    p.add_argument("--status", default="", help="推广活动状态ID逗号分隔（默认 4,9,11 = 含暂停；只算在投用 --status 9）")
    p.add_argument("--listed-only", action="store_true", help="[gap] 只输出「可直接补推」组（默认两组都出）")
    p.add_argument("--top", type=int, default=0, help="每组控制台只显示前 N 名（0=全部；CSV 始终写全量）")
    p.add_argument("--no-cn", action="store_true", help="不补全商品中文名（飞书表内已有的仍显示）")
    p.add_argument("--url", default="", help="飞书表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="飞书表名（默认 订单登记）")
    p.add_argument("--page-size", type=int, default=100, help="推广活动列表分页大小（默认 100）")
    p.add_argument("--max-pages", type=int, default=50, help="推广活动列表最多翻页数（默认 50）")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取 N 个活动（0=不限）")
    bind(p, 'sellerops.features.pricing.promo_gap', 'run')

    p = sub.add_parser("discount",
                           help="折扣改价（WB 原生批量，从高到低查询，支持 --name/--vc/--shops 灵活筛选，默认不做写后验证）")
    _add_discount_wb_args(p)
    bind(p, 'sellerops.features.pricing.discounts', 'run_cli')

    p = sub.add_parser("discount-bcs",
                           help="BCS 通道全量改折扣（较慢，按需手动调用）")
    p.add_argument("--apply", action="store_true", help="真正提交（默认 dry-run）")
    p.add_argument("--threshold", type=int, default=config.DISCOUNT_THRESHOLD_DEF,
                       help="阈值：只处理折扣>该值的商品；全量设目标值用 --threshold -1")
    p.add_argument("--target", type=int, default=config.DISCOUNT_TARGET_DEF, help="目标折扣")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")

    bind(p, 'sellerops.features.pricing.discount_bcs', 'run')

    p = sub.add_parser("price-review", help="价格审核：查看并应用新价格（降价 30-49.9%% 进审查的商品）")
    p.add_argument("--apply", action="store_true", help="真正应用新价格（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多审核 N 个（0=全部）")
    bind(p, 'sellerops.features.pricing.price_review', 'run')

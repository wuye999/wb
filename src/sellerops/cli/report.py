"""命令定义：report。"""
import argparse
from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('mapping', help='从本地库导出价格映射总表或单店表')
    p.add_argument('--shop-id', type=int, help='导出指定店铺；省略时导出总表')
    p.add_argument('--out', default='', help='输出路径（默认 exports/ 下的 .xlsx）')
    bind(p, 'sellerops.features.catalog.mapping_sync', 'run_export')

    p = sub.add_parser('products', help='只读：默认筛选低评分、高退货率及近期出单补货候选 → Excel')
    p.add_argument('--details', action='store_true', help='追加原始汇总、订单、评价和链接证据表（默认仅简洁筛选与覆盖说明）')
    p.add_argument('--shops', default='', help='本机目标店铺ID，逗号分隔（默认本机BCS账号全部店铺；不筛选临时账号或飞书来源）')
    p.add_argument('--extra-bcs-stdin', action='store_true',
                   help='从标准输入读取临时BCS账号的一个或多个fetch抓包，仅补充统计，凭证不落盘')
    p.add_argument('--order-days', type=int, default=30, help='退货统计下单窗口，含今天（1–31天，默认30）')
    p.add_argument('--feedback-days', type=int, default=0, help='评价日期窗口，含今天（默认0：接口可访问的全部历史）')
    p.add_argument('--wb-region', choices=['ru', 'cn'], default='ru', help='WB官方主机区域（默认ru；不自动跨域重试）')
    p.add_argument('--url', default='', help='飞书多维表格地址（默认feishu.base_url）')
    p.add_argument('--table', default='订单登记', help='飞书来源表（默认订单登记）')
    p.add_argument('--out', default='', help='输出.xlsx路径（默认exports/商品运营报告_时间.xlsx）')
    bind(p, 'sellerops.features.reports.product_report', 'run')

    p = sub.add_parser('vc-sales',
                           help="只读：飞书「订单登记」按供应商代码(BCS编号)统计单数并降序（跨店合并；支持店铺/日期过滤）")
    p.add_argument("--days", type=int, default=7, help="近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）")
    p.add_argument("--date", default="", help="单天 YYYY-MM-DD（等价 --begin=--end）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（单独给按单天处理）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）")
    p.add_argument("--shops", default="", help="店铺过滤，逗号分隔，支持店铺ID或短名（如 9352 或 袁州1；默认全部）")
    p.add_argument("--top", type=int, default=0, help="控制台只显示前 N 名（0=全部；CSV 始终写全量）")
    p.add_argument("--by-prefix", action="store_true", help="按 vendorCode 的 4 位前缀码聚合（默认完整 vendorCode）")
    p.add_argument("--no-cn", action="store_true", help="不补全商品中文名（表内已有的仍会显示）")
    p.add_argument("--url", default="", help="飞书表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="表名（默认 订单登记）")
    bind(p, 'sellerops.features.reports.feishu_vc_stats', 'run')

    p = sub.add_parser('delivery',
                           help="只读：WB FBS「配送中」清单及其全部商品采集+统计（按清单 createdAt 时段、"
                                "商品状态默认 shipment=待发货；控制台汇总 + CSV 明细）")
    p.add_argument("--shops", default="", help="店铺过滤，逗号分隔，支持店铺ID或短名（默认全部已填 cookie 店铺）")
    p.add_argument("--days", type=int, default=0, help="近 N 天（按清单 createdAt；0=不过滤，默认）")
    p.add_argument("--date", default="", help="单天 YYYY-MM-DD（等价 --begin=--end）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（单独给按单天处理）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）")
    p.add_argument("--item-state", default="shipment",
                       help="商品级状态过滤，逗号分隔（默认 shipment=待发货；all=不过滤；"
                            "可选 cancel/sold/readyForPickup/reshipment/sorted/"
                            "expectedSorting=等待分拣）")
    p.add_argument("--supply-state", default="",
                       help="清单级状态过滤，逗号分隔（默认不过滤；可选 complete/onway/reshipment/sort）")
    p.add_argument("--include-canceled", action="store_true", help="把 cancel 并入 --item-state（默认排除已取消）")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 个清单（0=不限）")
    p.add_argument("--max-pages", type=int, default=50, help="清单/商品列表翻页安全上限（默认 50，防死循环）")
    p.add_argument("--top", type=int, default=0, help="控制台按中文名只显示前 N 名（0=全部；CSV 始终全量）")
    p.add_argument("--no-cn", action="store_true", help="不解析商品中文名（供应商代码仍解析）")
    p.add_argument("--interval", type=float, default=0.3, help="清单间/店铺间请求间隔秒（默认 0.3）")
    bind(p, 'sellerops.features.reports.fbs_delivery', 'run')

    p = sub.add_parser('orders',
                           help="只读：WB「订单动态」近一月订单按商品中文名统计（订单数/退货数/退货率/"
                                "完单口径退货率/未完结）→ xlsx 多 Sheet（汇总 + 按供应商代码 + 每店 + 订单明细）")
    p.add_argument("--shops", default="", help="店铺过滤，逗号分隔，支持店铺ID或短名（默认全部已填 cookie 店铺）")
    p.add_argument("--days", type=int, default=30, help="近 N 天（含今天，默认 30；仅当未给 --begin/--end/--date 时生效）")
    p.add_argument("--date", default="", help="单天 YYYY-MM-DD（等价 --begin=--end）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（单独给按单天处理）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）")
    p.add_argument("--timezone", default="America/Los_Angeles",
                       help="接口时区（默认 America/Los_Angeles，对齐抓包；--days/--begin/--end 的日期口径同此，"
                            "LA 日期比北京时间晚约 15 小时）")
    p.add_argument("--page-size", type=int, default=50, help="每页条数（默认 50，抓包实测；服务端夹小会自动降级）")
    p.add_argument("--max-pages", type=int, default=200, help="每店翻页安全上限（默认 200，防死循环）")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取 N 行明细（0=不限）")
    p.add_argument("--top", type=int, default=0,
                       help="控制台两张表（按中文名 / 按供应商代码）各只显示前 N 名（0=全部；xlsx 始终全量）")
    p.add_argument("--no-cn", action="store_true", help="不解析商品中文名（供应商代码仍解析）")
    p.add_argument("--quiet", action="store_true", help="不打印逐页翻页进度")
    p.add_argument("--interval", type=float, default=0.3, help="翻页/店铺间请求间隔秒（默认 0.3）")
    p.add_argument("--out", default="", help="自定义 xlsx 路径（默认 exports/订单动态统计_<时间戳>.xlsx）")
    p.add_argument("--no-detail", action="store_true", help="不输出「订单明细」Sheet（行数大时提速、减小文件）")
    p.add_argument("--detail-max-rows", type=int, default=0, dest="detail_max_rows",
                       help="「订单明细」Sheet 最多写 N 行（0=不限）")
    p.add_argument("--no-local-filter", action="store_true", dest="no_local_filter",
                       help="不在本地按 created 复核时间窗（默认复核；越界行会显式告警）")
    bind(p, 'sellerops.features.reports.order_stats', 'run')

    p = sub.add_parser('ratings',
                           help="查询商品链接评分与评价情况（只读；支持按评分/店铺/1星评论筛选，导出包含价格表数据的报表）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部）")
    p.add_argument("--rating", type=float, default=None, help="精确筛选指定评分（如 1 或 1.0）")
    p.add_argument("--min-rating", type=float, default=None, help="最低评分（>=）")
    p.add_argument("--max-rating", type=float, default=None, help="最高评分（<=，如 1 或 2 筛选低分商品）")
    p.add_argument("--one-star", action="store_true", help="筛选存在 1 星评论的商品（oneStar >= 1）")
    p.add_argument("--two-star", action="store_true", help="筛选存在 2 星评论的商品（twoStar >= 1）")
    p.add_argument("--negative", "--bad", action="store_true", dest="negative",
                       help="筛选存在差评的商品（即 1 星或 2 星差评数 >= 1）")
    p.add_argument("--stars", default="", help="指定星级评论筛选（逗号分隔，如 1,2 筛选含 1 星或 2 星差评的商品）")
    p.add_argument("--min-one-star", type=int, default=0, help="筛选 1 星评论数 >= N 的商品")
    p.add_argument("--min-feedbacks", type=int, default=1, help="最低评价总数（默认 1，仅看有评价商品；设为 0 含无评价）")
    p.add_argument("--days", type=int, default=365, help="统计天数跨度（默认 365 天）")
    names = p.add_mutually_exclusive_group()
    names.add_argument("--name", default="", help="完整中文商品名")
    names.add_argument("--name-contains", help="中文商品名包含指定文字")
    p.add_argument("--prefix", default="", help="按 4 位前缀码筛选")
    p.add_argument("--vc", default="", help="按 vendorCode 筛选")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取商品条数（0=不限）")
    p.add_argument("--out", default="", help="自定义导出文件路径（支持 .csv / .xlsx）")
    bind(p, 'sellerops.features.reports.ratings', 'run')

    for parser in sub.choices.values():
        parser.add_argument('--json', action='store_true', help='输出单个 JSON 对象')

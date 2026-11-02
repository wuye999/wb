"""orders 命令定义。"""
import argparse

from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("orders", help="订单查询（自动同步 + 查询指定日期区间）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    p.add_argument("--days", type=int, default=0, help="查询最近 N 天（--begin/--end 未指定时，默认 1 天）")
    p.add_argument("--no-sync", action="store_true", help="跳过订单同步，直接查缓存")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部）")
    p.add_argument("--page-size", type=int, default=50, help="分页每页条数")
    bind(p, 'sellerops.features.orders.orders', 'run')

    p = sub.add_parser("mabang-orders", help="马帮待处理订单 SKU 匹配核对/更换（VC→映射表中文名→价格表库存SKU）")
    p.add_argument("--days", type=int, default=0, help="新增待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天")
    p.add_argument("--page-size", type=int, default=100, help="订单列表每页条数")
    p.add_argument("--apply", action="store_true", help="真正更换错误匹配（默认 dry-run 只出报表）")
    bind(p, 'sellerops.features.orders.matching', 'run')

    p = sub.add_parser("mabang-forecast",
                           help="马帮预报批次：已匹配商品订单生成预报批次并上传（dry-run 默认；--check 查批次状态）")
    p.add_argument("--days", type=int, default=0, help="新增待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天")
    p.add_argument("--page-size", type=int, default=100, help="订单列表每页条数")
    p.add_argument("--apply", action="store_true", help="全链路执行：生成批次→上传→轮询等待上传结束→设置交运方式（各步幂等跳过已完成项）")
    p.add_argument("--check", action="store_true", help="只查询预报批次（默认四 tab 概览：待预报/预报成功/预报失败/历史预报）")
    p.add_argument("--status", type=int, choices=[1, 3, 99, 5], default=None,
                       help="--check 只看单个 tab：1=待预报 / 3=预报成功 / 99=预报失败 / 5=历史预报（默认四 tab 概览）")
    p.add_argument("--pages", type=int, default=1, help="--check --status 时最多打印页数（默认 1；0=翻到底）")
    p.add_argument("--wait", type=int, default=0,
                       help="轮询之外额外的最小等待秒数（默认 0；旧「盲等固定秒数」语义已废弃）")
    p.add_argument("--upload-timeout", type=int, default=None,
                       help="轮询等待新批次上传结束的最长秒数（默认读配置 mabang.upload_timeout_seconds，缺省 900）")
    p.add_argument("--poll-interval", type=int, default=None,
                       help="批次状态轮询间隔秒数（默认读配置 mabang.upload_poll_interval，缺省 20）")
    p.add_argument("--upload-waiting", action="store_true", help="把待上传列表(status=1)中历史批次一并补传（补传后同样轮询等待上传结束）")
    bind(p, 'sellerops.features.orders.forecast', 'run_forecast')

    p = sub.add_parser("feishu-register", help="马帮订单登记到飞书多维表格（按订单编号去重，dry-run 默认）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="表格名（默认 订单登记）")
    p.add_argument("--scope", choices=["latest", "pending", "all"], default="latest",
                       help="latest=最近500条全状态去重只登新增（默认）；pending=待处理订单；all=全部状态订单（补录历史，配合 --date 或 --begin/--end）")
    p.add_argument("--date", default="", help="单天日期 YYYY-MM-DD（--scope all 用）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD（--scope all 用）")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（--scope all 用）")
    p.add_argument("--days", type=int, default=0, help="待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天；用于 pending 或 latest 合并待处理单")
    p.add_argument("--page-size", type=int, default=100, help="订单列表每页条数")
    p.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run 只列出将登记订单）")
    p.add_argument("--no-pending", action="store_true",
                       help="不合并「待处理订单」数据源（默认 scope=latest 会合并 orderalllist + 待处理订单，"
                            "让只匹配但未进预报/上传/交运流程的订单也能登记）")
    bind(p, 'sellerops.features.orders.feishu_register', 'run')

    p = sub.add_parser("feishu-register-dedup",
                           help="存量修复：「订单登记」表内重复订单编号清理"
                                "（按订单编号分组，每组保留表内最早一条、其余删除；dry-run 默认）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="表格名（默认 订单登记）")
    p.add_argument("--apply", action="store_true", help="真正删除重复记录（默认 dry-run 只出报告）")
    p.add_argument("--limit", type=int, default=0, help="本次最多删除 N 行（0=不限）")
    bind(p, 'sellerops.features.orders.feishu_register_dedup', 'run')

    p = sub.add_parser("mabang-stock-register",
                           help="拉取马帮全部库存 SKU → 全量重建飞书「马帮库存登记表」（含附件列「图」）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="马帮库存登记表", help="目标表名（默认 马帮库存登记表）")
    p.add_argument("--apply", action="store_true", help="真正全量重建（默认 dry-run 预览）")
    bind(p, 'sellerops.features.orders.mabang_stock', 'run')

    p = sub.add_parser("mabang-stock-daily",
                           help="「马帮库存登记表」日期列管理：默认只建今天列+更新全部已有日期列；--begin/--date 任一显式给出即进入区间模式（删早于该日的旧列+补建区间缺列，--end 需同用）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="马帮库存登记表", help="库存表名（默认 马帮库存登记表）")
    p.add_argument("--orders-table", default="订单登记", help="订单明细表名（默认 订单登记）")
    p.add_argument("--date", default="", help="单天日期 YYYY-MM-DD（默认今天；显式给出即进入区间模式，会删除该日之前的旧日期列）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（显式给出即删除该日之前的旧日期列，并补建至 --end 的缺列）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin/--date 同用，单独使用直接报错）")
    p.add_argument("--apply", action="store_true", help="真正建列并填充（默认 dry-run 预览）")
    bind(p, 'sellerops.features.orders.mabang_stock_daily', 'run_daily')

    p = sub.add_parser("feishu-vc-stats",
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
    bind(p, 'sellerops.features.orders.feishu_vc_stats', 'run')

    p = sub.add_parser("fbs-delivery",
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
    bind(p, 'sellerops.features.orders.fbs_delivery', 'run')

    p = sub.add_parser("order-stats",
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
    p.add_argument("--out", default="", help="自定义 xlsx 路径（默认 data/logs/订单动态统计_<时间戳>.xlsx）")
    p.add_argument("--no-detail", action="store_true", help="不输出「订单明细」Sheet（行数大时提速、减小文件）")
    p.add_argument("--detail-max-rows", type=int, default=0, dest="detail_max_rows",
                       help="「订单明细」Sheet 最多写 N 行（0=不限）")
    p.add_argument("--no-local-filter", action="store_true", dest="no_local_filter",
                       help="不在本地按 created 复核时间窗（默认复核；越界行会显式告警）")
    bind(p, 'sellerops.features.orders.order_stats', 'run')

    p = sub.add_parser("mabang-process",
                           help="马帮订单处理一体：匹配商品→预报单→上传（自动发货）→物流交运→飞书登记（登记含只匹配未进预报流程的订单）")
    p.add_argument("--days", type=int, default=0, help="配置店铺的全部新增待处理订单，0=不限日期（默认）；正数=最近 N 天")
    p.add_argument("--page-size", type=int, default=100, help="订单列表分页大小（默认 100）")
    p.add_argument("--wait", type=int, default=0,
                       help="轮询之外额外的最小等待秒数（默认 0；旧「盲等固定秒数」语义已废弃）")
    p.add_argument("--upload-timeout", type=int, default=None,
                       help="轮询等待新批次上传结束的最长秒数（默认读配置 mabang.upload_timeout_seconds，缺省 900）")
    p.add_argument("--poll-interval", type=int, default=None,
                       help="批次状态轮询间隔秒数（默认读配置 mabang.upload_poll_interval，缺省 20）")
    p.add_argument("--url", default="", help="飞书表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="飞书订单登记表名（默认 订单登记）")
    p.add_argument("--no-pending", action="store_true",
                       help="飞书登记时不合并「待处理订单」数据源（默认合并，覆盖只匹配未进预报/上传/交运的订单）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run 预览）")
    bind(p, 'sellerops.features.orders.mabang_process', 'run')

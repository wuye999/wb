"""命令定义：order。"""
import argparse
from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('list', help="订单查询（自动同步 + 查询指定日期区间）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    p.add_argument("--days", type=int, default=0, help="查询最近 N 天（--begin/--end 未指定时，默认 1 天）")
    p.add_argument("--no-sync", action="store_true", help="跳过订单同步，直接查缓存")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部）")
    p.add_argument("--page-size", type=int, default=50, help="分页每页条数")
    bind(p, 'sellerops.features.orders.orders', 'run')

    p = sub.add_parser('match', help="按价格表发货方式分流待处理订单，匹配/更换马帮库存SKU")
    p.add_argument("--days", type=int, default=0, help="新增待处理订单日期范围：0=不限日期（默认）；正数=最近 N 天")
    p.add_argument("--page-size", type=int, default=100, help="订单列表每页条数")
    p.add_argument("--apply", action="store_true", help="真正更换错误匹配（默认 dry-run 只出报表）")
    bind(p, 'sellerops.features.orders.matching', 'run')

    p = sub.add_parser('forecast',
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

    p = sub.add_parser('register', help="马帮订单登记到飞书多维表格（按订单编号去重，dry-run 默认）")
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
    p.add_argument("--json", action="store_true", help="输出包含登记计数的 JSON 回执")
    bind(p, 'sellerops.features.orders.feishu_register', 'run')

    p = sub.add_parser('deduplicate',
                           help="存量修复：「订单登记」表内重复订单编号清理"
                                "（按订单编号分组，每组保留表内最早一条、其余删除；dry-run 默认）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="表格名（默认 订单登记）")
    p.add_argument("--apply", action="store_true", help="真正删除重复记录（默认 dry-run 只出报告）")
    p.add_argument("--limit", type=int, default=0, help="本次最多删除 N 行（0=不限）")
    bind(p, 'sellerops.features.orders.feishu_register_dedup', 'run')

    p = sub.add_parser('stock-register',
                           help="拉取马帮全部库存 SKU → 全量重建飞书「马帮库存登记表」（含附件列「图」）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="马帮库存登记表", help="目标表名（默认 马帮库存登记表）")
    p.add_argument("--apply", action="store_true", help="真正全量重建（默认 dry-run 预览）")
    bind(p, 'sellerops.features.orders.mabang_stock', 'run')

    p = sub.add_parser('stock-days',
                           help="「马帮库存登记表」日期列管理：默认只建今天列+更新全部已有日期列；--begin/--date 任一显式给出即进入区间模式（删早于该日的旧列+补建区间缺列，--end 需同用）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="马帮库存登记表", help="库存表名（默认 马帮库存登记表）")
    p.add_argument("--orders-table", default="订单登记", help="订单明细表名（默认 订单登记）")
    p.add_argument("--date", default="", help="单天日期 YYYY-MM-DD（默认今天；显式给出即进入区间模式，会删除该日之前的旧日期列）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（显式给出即删除该日之前的旧日期列，并补建至 --end 的缺列）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin/--date 同用，单独使用直接报错）")
    p.add_argument("--apply", action="store_true", help="真正建列并填充（默认 dry-run 预览）")
    bind(p, 'sellerops.features.orders.mabang_stock_daily', 'run_daily')

    p = sub.add_parser('group-ship',
                       help="WB 中国区 FBS 发货群供货：按店铺和中文名建供货、转配送并导出 PDF")
    p.add_argument("--shops", default="", help="限定店铺 ID，逗号分隔（默认 BCS 全部店铺）")
    p.add_argument("--name", default="", help="精确限定一个商品中文名（默认全部标记商品）")
    p.add_argument("--resume", action="store_true", help="查看已受理配送的待导出供货；加 --apply 补取 PDF")
    p.add_argument("--apply", action="store_true", help="执行供货和导出（默认预览）；与 --resume 同用时仅补取 PDF")
    bind(p, 'sellerops.features.orders.fbs_group_ship', 'run')

    p = sub.add_parser('process',
                           help="马帮订单处理一体：匹配商品→预报单→上传（自动发货）→物流交运；有飞书地址和 lark-cli 时登记")
    p.add_argument("--days", type=int, default=0, help="配置店铺的全部新增待处理订单，0=不限日期（默认）；正数=最近 N 天")
    p.add_argument("--page-size", type=int, default=100, help="订单列表分页大小（默认 100）")
    p.add_argument("--wait", type=int, default=0,
                       help="轮询之外额外的最小等待秒数（默认 0；旧「盲等固定秒数」语义已废弃）")
    p.add_argument("--upload-timeout", type=int, default=None,
                       help="轮询等待新批次上传结束的最长秒数（默认读配置 mabang.upload_timeout_seconds，缺省 900）")
    p.add_argument("--poll-interval", type=int, default=None,
                       help="批次状态轮询间隔秒数（默认读配置 mabang.upload_poll_interval，缺省 20）")
    p.add_argument("--url", default="", help="飞书表格地址（默认读配置 feishu.base_url；缺地址或 lark-cli 时跳过登记）")
    p.add_argument("--table", default="订单登记", help="飞书订单登记表名（默认 订单登记）")
    p.add_argument("--no-pending", action="store_true",
                       help="飞书登记时不合并「待处理订单」数据源（默认合并，覆盖只匹配未进预报/上传/交运的订单）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run 预览）")
    p.add_argument("--json", action="store_true", help="输出 JSON 回执，尝试登记时包含登记计数")
    bind(p, 'sellerops.features.orders.mabang_process', 'run')

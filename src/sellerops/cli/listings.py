"""listings 命令定义。"""
import argparse

from sellerops.cli.binding import bind
from sellerops.cli.arguments import _add_ops_args, _add_shelve_args


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("price", help="改价/改折扣（默认 WB 原生 dp-api 批量，自动确认降价/隔离区弹窗；dry-run 默认）")
    _add_ops_args(p, with_price=True)
    p.add_argument("--chunk", type=int, default=100, help="每批提交条数（≤300，对齐 discount 实践，默认 100）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")
    bind(p, 'sellerops.features.listings.prices', 'run')

    p = sub.add_parser("price-bcs", help="[备选] 改价走 BCS 接口（price/batch，WB 原生不可用时的兜底）")
    _add_ops_args(p, with_price=True)
    bind(p, 'sellerops.features.listings.operations', 'run', action='price')

    p = sub.add_parser("stock", help="改库存（默认 WB 原生在线接口；dry-run 默认，归零须 --yes）")
    _add_ops_args(p, with_stock=True)
    p.add_argument("--chunk", type=int, default=500, help="每批提交条数（≤1000，探针实测 1000 可行，默认 500）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")
    p.add_argument("--max-pages", type=int, default=200, help="WB 游标分页安全上限（--resolve live 时使用）")
    p.add_argument("--resolve", choices=["snapshot", "live"], default="snapshot",
                       help="chrtId 解析源：snapshot=BCS 快照（默认，零请求）；live=WB portal/stocks 实时拉取（绕过快照滞后）")
    bind(p, 'sellerops.features.listings.stocks', 'run')

    p = sub.add_parser("stock-bcs", help="[备选] 改库存走 BCS 接口（stock/batchSetByChrtIdsBatch，WB 原生不可用时的兜底）")
    _add_ops_args(p, with_stock=True)
    bind(p, 'sellerops.features.listings.operations', 'run', action='stock')

    p = sub.add_parser("trash", help="下架（默认 WB 原生 moveNmsToTrash 移回收站；先清库存；不可逆）")
    _add_ops_args(p)
    p.add_argument("--chunk", type=int, default=300, help="每批移回收站条数（≤300，默认 300）")
    p.add_argument("--interval", type=float, default=0.3, help="批间间隔秒")
    bind(p, 'sellerops.features.listings.operations', 'run', action='trash', channel='wb')

    p = sub.add_parser("trash-bcs", help="[旧通道] 移回收站走 BCS 接口（clean/removeToTrash）；仅供排查，**不作兜底**（BCS 不可靠）")
    _add_ops_args(p)
    p.add_argument("--chunk", type=int, default=300, help="每批条数（≤300，默认 300）")
    p.add_argument("--interval", type=float, default=0.3,
                       help="批间间隔秒（BCS 有 19s 限流窗口，连续多批建议 21）")
    bind(p, 'sellerops.features.listings.operations', 'run', action='trash', channel='bcs')

    p = sub.add_parser("replicate", help="跨店复制上架：把部分覆盖的商品上架到缺失店铺（dry-run 默认）")
    p.add_argument("--vc", default="", help="指定 vendorCode（逗号分隔）")
    p.add_argument("--prefix", default="", help="前缀码筛选（4 位大写）")
    p.add_argument("--name", default="", help="映射表中文名包含匹配")
    p.add_argument("--shops", default="", help="限定目标店铺 id 逗号分隔（默认全部缺失店）")
    p.add_argument("--source-shops", default="", help="源店铺 id 逗号分隔（默认全部本地店铺，按完整 VC 去重）")
    p.add_argument("--price-source", choices=["snapshot", "minimum"], default="snapshot",
                   help="价格来源：snapshot=源店快照价格；minimum=商品价格表最低售价")
    p.add_argument("--price-multiplier", type=float, default=1.0, help="价格来源乘数，按分四舍五入（默认 1）")
    p.add_argument("--stock", type=int, default=999, help="上架库存（默认999，可显式设为0）")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个 vc（0=不限）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")


    p.add_argument("--interval", type=float, default=1.0, help="批次上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")
    bind(p, 'sellerops.features.listings.replicate', 'run')

    p = sub.add_parser("import-publish", help="他人映射表导入上架：他人有我方无的商品（按真实 WB商品码 匹配）上架到我的店铺")
    p.add_argument("xlsx", help="他人映射表 xlsx 路径（同项目「映射总表」格式）")
    p.add_argument("--cn", default="", help="他人表中文名包含过滤（逗号分隔多个）")
    p.add_argument("--shops", default="", help="限定目标店铺 id 逗号分隔（默认全部店铺）")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个商品（0=不限）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")


    p.add_argument("--interval", type=float, default=1.0, help="批次上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")
    bind(p, 'sellerops.features.listings.import_publish', 'run')

    p = sub.add_parser("dimension", help="批量修改尺寸（按价格表或 --dims 自定义，dry-run 默认，--apply 执行）")
    p.add_argument("--vc", default="", help="指定单个或多个 vendorCode（逗号分隔）")
    p.add_argument("--prefix", default="", help="vendorCode 4位前缀码包含匹配")
    p.add_argument("--name", default="", help="商品价格表产品中文名包含匹配")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    p.add_argument("--dims", default="", help="自定义尺寸 '长*宽*高/毛重'（例: 8*14*26/0.3）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")

    bind(p, 'sellerops.features.listings.dimension', 'run')

    p = sub.add_parser("dims-check", help="查询 WB 包装尺寸/重量偏差待验证商品（只读）")
    p.add_argument("--type", choices=["dims", "weight", "all"], default="dims",
                       help="dims=尺寸偏差 / weight=重量偏差 / all=两者合并去重（默认 dims）")
    p.add_argument("--name", default="", help="映射表中文名包含过滤")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多查询 N 条（0=不限）")
    bind(p, 'sellerops.features.listings.dims_check', 'run')

    p = sub.add_parser("banned", help="查询并删除被阻止的商品（dry-run 默认，--apply 移到回收站）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    p.add_argument("--yes", action="store_true", help="跳过不可逆确认（移到回收站）")

    bind(p, 'sellerops.features.listings.banned', 'run')

    p = sub.add_parser("clean", help="清理草稿箱/回收站")
    p.add_argument("--target", required=True, choices=["basket", "draft", "all"],
                       help="basket=回收站 / draft=草稿箱 / all=先草稿后回收站")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条")

    bind(p, 'sellerops.features.listings.clean', 'run')

    p = sub.add_parser("warehouse-clear", help="成都仓库商品永久删除（当前仅操作莫斯科，不使用此命令）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部店铺的成都仓）")
    p.add_argument("--limit", type=int, default=0, help="（预留）每店最多处理 N 条，0=全部")
    p.add_argument("--interval", type=float, default=0.3, help="删除请求间隔秒（默认 0.3）")
    p.add_argument("--parallel", type=int, default=1, help="并发店铺数（默认 1=串行；--apply 时有效，店内仍串行）")
    p.add_argument("--apply", action="store_true", help="真正执行删除（不可逆）")
    p.add_argument("--yes", action="store_true", help="确认永久删除（后台运行必备）")
    bind(p, 'sellerops.features.listings.warehouse_clear', 'run')

    p = sub.add_parser("publish", help="批量发布上架：输入 WB 商品码推送到店铺（支持指定前缀/完整VC/价格/尺寸/店铺）")
    _add_shelve_args(p)
    bind(p, 'sellerops.features.listings.bulk_publish', 'run')

    p = sub.add_parser("create-cards", help="建卡发布上架：输入 WB 商品码推送到店铺（支持自定义完整VC/俄文详情与主图建卡）")
    _add_shelve_args(p)
    bind(p, 'sellerops.features.listings.card_publish', 'run')

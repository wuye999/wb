"""命令定义：catalog。"""
import argparse
from sellerops.cli.arguments import nonempty, shop_ids
from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('fetch', help='拉取商品快照')
    add_fetch_args(p)

    p = sub.add_parser('mapping', help="生成带图待认领工作台（同 map workbench）")
    p.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    bind(p, 'sellerops.features.catalog.workbench', 'run_workbench')

    add_mismatch_args(sub.add_parser('check-mismatch', help='货不对板大图画廊（同 map check-mismatch）'))

    p = sub.add_parser('merge', help="保存待认领工作台审核结果（同 map batch-set --file）")
    p.add_argument("file", help="审核结果 JSON 数组文件路径")
    p.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    bind(p, 'sellerops.features.catalog.vc_mapping', 'run_batch_set')

    p = sub.add_parser('rename', help="纠偏/修改商品中文名（按卖家 SKU 重绑）")
    p.add_argument("--vc", default="", help="要改名的 vendorCode")
    p.add_argument("--cn", default="", help="新产品中文名")
    p.add_argument("--reason", default="人工纠偏", help="改名原因说明")
    p.add_argument("--file", default="", help="批量改名 JSON 文件路径（含 [{'vc': ..., 'cn': ...}]）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping_rename')

    p = sub.add_parser('shop-mappings', help="刷新/生成各店铺独立映射表（exports/shop_*.xlsx）")
    p.add_argument("--shop-id", type=int, default=None, help="指定店铺ID（默认全部活跃店铺）")
    p.add_argument("--force", action="store_true", help="兼容旧参数；导出总是读取当前库")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_shops_mapping')

    p = sub.add_parser('export-products', help='导出店铺标准商品表（.xlsx，不含底价；默认按源数字码跨店去重，便于查看与分享上架）')
    shops = p.add_mutually_exclusive_group(required=True)
    shops.add_argument('--shops', type=shop_ids, help='来源店铺 ID，逗号分隔')
    shops.add_argument('--all-shops', action='store_true', help='导出全部活跃店铺')
    p.add_argument('--out', help='输出路径 .xlsx（单店默认 exports/shop_<shop_id>_products.xlsx，多店或 --all-shops 默认 exports/shops_products.xlsx）')
    bind(p, 'sellerops.features.catalog.export_products', 'run')

    p = sub.add_parser('product-summary', help='离线按商品中文名汇总在架货盘的价格、折扣和默认业务仓库存')
    p.add_argument('--shops', type=shop_ids, help='店铺 ID，逗号分隔（默认全部在营店铺）')
    names = p.add_mutually_exclusive_group()
    names.add_argument('--name', type=nonempty, help='精确匹配完整中文名')
    names.add_argument('--name-contains', type=nonempty, help='匹配中文名子串')
    p.add_argument('--sort', choices=('count', 'shops', 'name'), default='count',
                   help='按商品条数降序、覆盖店数降序或中文名升序（默认 count）')
    p.add_argument('--limit', type=int, default=0, help='最多返回 N 个中文名组（0=不限）')
    p.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    bind(p, 'sellerops.features.catalog.product_summary', 'run')


def add_mismatch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cn", default="", help="只渲染指定中文名（精确匹配，如『牙膏-紫色』）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD（按商品创建时间筛选）")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（缺省=今天）")
    p.add_argument("--days", type=int, default=0, help="最近 N 天（含今天；与 --begin/--end 冲突时以具体日期为准）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mismatch_check')


def add_fetch_args(p: argparse.ArgumentParser, *, all_scope: bool = False) -> None:
    targets = p.add_mutually_exclusive_group()
    if all_scope:
        targets.add_argument('--all', action='store_true', help='同步全部在营店铺')
    targets.add_argument("--shop-id", type=int, default=None, help="店铺ID（默认全部）")
    p.add_argument('--source', choices=['bcs', 'wb-openapi'], default='bcs',
                   help='快照来源（默认bcs快照；wb-openapi需显式选择官方读取，不自动回退）')
    p.add_argument('--parallel', type=int, default=4,
                   help='WB Open API并发店铺数（正整数，默认4；1为串行，单店及BCS来源不受影响）')
    p.add_argument('--bcs-parallel', type=int, choices=(1, 2, 3, 4), default=2,
                   help='全店 BCS 后台同步并发数（默认2，单店和缓存读取不受影响）')
    p.add_argument("--no-sync", action="store_true",
                   help="BCS来源：跳过后台同步，直接读取缓存；Open API来源无需同步")
    p.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    bind(p, 'sellerops.cli.catalog_handlers', 'run_fetch')

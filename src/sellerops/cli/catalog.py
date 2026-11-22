"""命令定义：catalog。"""
import argparse
from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('fetch', help='拉取商品快照')
    add_fetch_args(p)

    p = sub.add_parser('mapping', help="生成统一核对工作台（全部活跃店铺并集，一页两区）")
    p.add_argument("--main-shop-only", action="store_true", help="只使用主店候选商品")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping')

    p = sub.add_parser('import-mapping', help="导入核对结果 → 保存归属（旧格式）")
    p.add_argument("file", help="核对结果 JSON")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping_import')

    p = sub.add_parser('check-mapping', help="映射表核查工作台（带图，核对匹配是否有误）")
    p.add_argument("--tol", type=int, default=5, help="价格偏差阈值（元，默认 5）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping_check')

    p = sub.add_parser('check-mismatch', help="货不对板筛查工作台（看图勾选，导出 vc 下架）")
    p.add_argument("--cn", default="", help="只渲染指定中文名（精确匹配，如『牙膏-紫色』）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD（按映射表创建时间/上架时间筛选）")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（缺省=今天）")
    p.add_argument("--days", type=int, default=0, help="最近 N 天（含今天；与 --begin/--end 冲突时以具体日期为准）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mismatch_check')

    p = sub.add_parser('review', help="多店铺待审核工作台")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_review')

    p = sub.add_parser('merge', help="保存审核确认的归属（file 可选）")
    p.add_argument("file", nargs="?", default=None, help="本次审核结果 JSON（可选）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_merge')

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

def add_fetch_args(p: argparse.ArgumentParser, *, all_scope: bool = False) -> None:
    targets = p.add_mutually_exclusive_group()
    if all_scope:
        targets.add_argument('--all', action='store_true', help='同步全部在营店铺')
    targets.add_argument("--shop-id", type=int, default=None, help="店铺ID（默认全部）")
    p.add_argument('--source', choices=['bcs', 'wb-openapi'], default='bcs',
                   help='快照来源（默认bcs快照；wb-openapi需显式选择官方读取，不自动回退）')
    p.add_argument('--parallel', type=int, default=4,
                   help='WB Open API并发店铺数（正整数，默认4；1为串行，单店及BCS来源不受影响）')
    p.add_argument("--no-sync", action="store_true",
                   help="BCS来源：跳过后台同步直接读取已同步快照；Open API来源无需同步")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_fetch')

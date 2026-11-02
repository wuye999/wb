"""catalog 命令定义。"""
import argparse

from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("shops", help="账号店铺列表")
    bind(p, 'sellerops.integrations.bcs.client', 'print_shops')

    p = sub.add_parser("fetch", help="拉取店铺商品数据（自动先同步 WB）")
    p.add_argument("--shop-id", type=int, default=None, help="店铺ID（默认全部）")
    p.add_argument("--no-sync", action="store_true", help="跳过 BCS 同步，直接拉取上次数据")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_fetch')

    p = sub.add_parser("mapping", help="生成统一核对工作台（全部活跃店铺并集，一页两区）")
    p.add_argument("--main-shop-only", action="store_true", help="只使用主店候选商品")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping')

    p = sub.add_parser("mapping-import", help="导入核对结果 → 生成映射表（旧格式）")
    p.add_argument("file", help="核对结果 JSON")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping_import')

    p = sub.add_parser("mapping-check", help="映射表核查工作台（带图，核对匹配是否有误）")
    p.add_argument("--tol", type=int, default=5, help="价格偏差阈值（元，默认 5）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping_check')

    p = sub.add_parser("mismatch-check", help="货不对板筛查工作台（看图勾选，导出 vc 下架）")
    p.add_argument("--cn", default="", help="只渲染指定中文名（精确匹配，如『牙膏-紫色』）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD（按映射表创建时间/上架时间筛选）")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（缺省=今天）")
    p.add_argument("--days", type=int, default=0, help="最近 N 天（含今天；与 --begin/--end 冲突时以具体日期为准）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mismatch_check')

    p = sub.add_parser("review", help="多店铺待审核工作台")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_review')

    p = sub.add_parser("merge", help="增量合并审核 → 映射表（file 可选）")
    p.add_argument("file", nargs="?", default=None, help="本次审核结果 JSON（可选）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_merge')

    p = sub.add_parser("mapping-rename", help="纠偏/修改商品中文名（自动级联更新全部店铺单表与聚合总表）")
    p.add_argument("--vc", default="", help="要改名的 vendorCode")
    p.add_argument("--cn", default="", help="新产品中文名")
    p.add_argument("--reason", default="人工纠偏", help="改名原因说明")
    p.add_argument("--file", default="", help="批量改名 JSON 文件路径（含 [{'vc': ..., 'cn': ...}]）")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_mapping_rename')

    p = sub.add_parser("shops-mapping", help="刷新/生成各店铺独立映射表（data/shops/shop_*.xlsx）")
    p.add_argument("--shop-id", type=int, default=None, help="指定店铺ID（默认全部活跃店铺）")
    p.add_argument("--force", action="store_true", help="强制全量重新构建")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_shops_mapping')

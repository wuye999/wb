"""catalog 命令定义。"""
import argparse

from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("shops", help="账号店铺列表")
    bind(p, 'sellerops.integrations.bcs.client', 'print_shops')

    p = sub.add_parser('product-report', help='只读：默认筛选低评分、高退货率及近期出单补货候选 → Excel')
    p.add_argument('--details', action='store_true', help='追加原始汇总、订单、评价和链接证据表（默认仅简洁筛选与覆盖说明）')
    p.add_argument('--shops', default='', help='本机目标店铺ID，逗号分隔（默认本机BCS账号全部店铺；不筛选临时账号或飞书来源）')
    p.add_argument('--extra-bcs-stdin', action='store_true',
                   help='从标准输入读取临时BCS账号的一个或多个fetch抓包，仅补充统计，凭证不落盘')
    p.add_argument('--order-days', type=int, default=30, help='退货统计下单窗口，含今天（1–31天，默认30）')
    p.add_argument('--feedback-days', type=int, default=0, help='评价日期窗口，含今天（默认0：接口可访问的全部历史）')
    p.add_argument('--wb-region', choices=['ru', 'cn'], default='ru', help='WB官方主机区域（默认ru；不自动跨域重试）')
    p.add_argument('--url', default='', help='飞书多维表格地址（默认feishu.base_url）')
    p.add_argument('--table', default='订单登记', help='飞书来源表（默认订单登记）')
    p.add_argument('--out', default='', help='输出.xlsx路径（默认data/logs/商品运营报告_时间.xlsx）')
    bind(p, 'sellerops.features.catalog.product_report', 'run')

    p = sub.add_parser("fetch", help="拉取商品快照（默认 WB 官方 API，仅从 BCS 读取店铺密钥）")
    p.add_argument("--shop-id", type=int, default=None, help="店铺ID（默认全部）")
    p.add_argument('--source', choices=['wb-openapi', 'bcs'], default='wb-openapi',
                   help='快照来源（默认wb-openapi；bcs显式选择旧缓存流程，不自动回退）')
    p.add_argument('--parallel', type=int, default=4,
                   help='Open API并发店铺数（正整数，默认4；1为串行，单店及BCS来源不受影响）')
    p.add_argument("--no-sync", action="store_true", help="BCS来源：跳过同步；Open API来源无需同步")
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

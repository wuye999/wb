import argparse
from typing import Any


def shop_ids(value: str) -> str:
    ids = value.split(",")
    if any(not item.strip().isdecimal() or int(item) <= 0 for item in ids):
        raise argparse.ArgumentTypeError("请填写有效店铺 ID，如 1001,1002")
    normalized = [str(int(item)) for item in ids]
    if len(set(normalized)) != len(normalized):
        raise argparse.ArgumentTypeError("店铺 ID 重复")
    return ",".join(normalized)


def nonempty(value: str) -> str:
    if not value.strip() or any(not part.strip() for part in value.split(",")):
        raise argparse.ArgumentTypeError("筛选值不能为空")
    return value.strip()


def add_shop_scope(parser):
    shops = parser.add_mutually_exclusive_group(required=True)
    shops.add_argument("--shops", type=shop_ids, help="目标店铺 ID，多个用逗号分隔")
    shops.add_argument("--all-shops", action="store_true", help="本动作的全部目标店铺")


def add_product_scope(parser):
    add_shop_scope(parser)
    items = parser.add_mutually_exclusive_group(required=True)
    items.add_argument("--sku", type=nonempty, help="商品价格表卖家 SKU")
    items.add_argument("--name", type=nonempty, help="完整中文商品名")
    items.add_argument("--name-contains", type=nonempty, help="中文商品名包含指定文字")
    items.add_argument("--prefix", type=nonempty, help="商品价格表前缀码")
    items.add_argument("--vc", type=nonempty, help="完整 VC，多个用逗号分隔")
    items.add_argument("--all-products", action="store_true", help="目标店铺全部在架商品，保留人工排除记录（全店折扣按平台清单）")


def add_ops_args(p: argparse.ArgumentParser, *, with_price: bool = False, with_stock: bool = False) -> None:
    """为 price/stock/trash 等 ops 类命令添加统一参数（互斥筛选组 + 通用开关）。

    Args:
        p: 目标子命令 parser。
        with_price: 追加改价相关参数（--price/--discount/--club-discount/--keep-price/--auto-review）。
        with_stock: 追加库存参数（--amount）。

    Returns:
        None（原地修改 parser）。
    """
    add_product_scope(p)
    p.add_argument("--apply", action="store_true", help="兼容保留；确认预览生成的计划单后，用 apply --plan 提交")
    p.add_argument("--yes", action="store_true", help="兼容保留；确认计划单后，用 apply --plan 提交")

    if with_price:
        intent = p.add_mutually_exclusive_group()
        intent.add_argument("--price", type=int, help="指定正整数价格")
        intent.add_argument("--keep-price", action="store_true", help="保持各规格原价，须同时指定折扣")
        p.add_argument("--discount", type=int, help="折扣 0-100，配合 --price 或 --keep-price")
        p.add_argument("--club-discount", type=int, help="BCS 俱乐部折扣 0-100，须明确价格意图")
        p.add_argument("--auto-review", action="store_true",
                       help="改价后自动「应用新价格」（降价 30-49.9%% 进审查时，精确匹配刚改价商品）")
    if with_stock:
        p.add_argument("--amount", type=int, required=True, help="目标库存，归零传 0")


def add_discount_args(parser):
    add_product_scope(parser)
    parser.add_argument("--channel", choices=["wb", "bcs"], default="wb")
    parser.add_argument("--apply", action="store_true", help="兼容保留；确认预览生成的计划单后，用 apply --plan 提交")
    parser.add_argument("--target", type=int, required=True, help="目标折扣，必填（0 至 100）")
    parser.add_argument("--threshold", type=int, help="仅修改平台当前折扣严格高于此值的商品")
    parser.add_argument("--below", type=int, default=-1, help="也修改平台当前折扣严格低于此值的商品（WB）")
    parser.add_argument("--limit", type=int, default=0, help="每店最多处理的商品数")
    parser.add_argument("--chunk", type=int, help="每批商品数（仅 WB，默认 100）")


def add_publish_args(parser):
    parser.add_argument("nms", nargs="*", help="WB 商品码列表（支持空格或逗号分隔，如 123456789 123456790）")
    parser.add_argument("--nm", default="", help="WB 商品码（逗号分隔多个，兼容命令行参数传参）")
    parser.add_argument("--vc", default="", help="完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）")
    parser.add_argument("--prefix", default="", help="4位前缀码（如 ABCD 或 BCS-ABCD）")
    parser.add_argument("--sku", default="", help="商品价格表中的卖家 SKU，适用于本次输入商品")
    parser.add_argument("--random-prefix", action="store_true", help="没有唯一登记前缀时，明确允许在预览时随机生成")
    parser.add_argument("--price", type=float, default=None, help="指定售价（CNY），不填时使用本地快照价")
    parser.add_argument("--dims", default="", help="自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）")
    parser.add_argument("--length", type=int, default=None, help="包装长 (cm)")
    parser.add_argument("--width", type=int, default=None, help="包装宽 (cm)")
    parser.add_argument("--height", type=int, default=None, help="包装高 (cm)")
    parser.add_argument("--weight", type=float, default=None, help="包装毛重 (kg)")
    add_shop_scope(parser)
    parser.add_argument("--stock", type=int, default=999, help="上架库存量（默认 999）")
    parser.add_argument("--cn", default="", help="商品中文名（辅助匹配价格与包装规格）")
    parser.add_argument("--file", default="", help="批量上架文件路径（支持 .txt 每行一个商品码、.json 商品列表或 .csv 表格）")
    parser.add_argument("--cn-stock", default="", help="按中文名指定库存（'中文名:库存,...'）")
    parser.add_argument("--interval", type=float, default=1.0, help="批次/商品请求间隔秒（默认 1.0）")
    parser.add_argument("--apply", action="store_true", help="兼容保留；确认预览生成的计划单后，用 apply --plan 提交")

"""
sellerops CLI 参数定义公共件（argparse-only，无任何业务依赖）

为什么放在 framework：`cli.py` 需要「启动零业务依赖」（40 个命令延迟加载），
而 `services/replicate/ops.py` 也需要同一套 ops 参数定义 —— 把唯一实现放在这里，
两边共用，避免同一份 argparse 定义在 cli 与 service 里各写一遍（重复实现）。
"""
import argparse
from typing import Any


def add_ops_args(p: argparse.ArgumentParser, *, with_price: bool = False, with_stock: bool = False) -> None:
    """为 price/stock/trash 等 ops 类命令添加统一参数（互斥筛选组 + 通用开关）。

    Args:
        p: 目标子命令 parser。
        with_price: 追加改价相关参数（--price/--discount/--club-discount/--keep-price/--auto-review）。
        with_stock: 追加库存参数（--amount）。

    Returns:
        None（原地修改 parser）。
    """
    g = p.add_mutually_exclusive_group()
    g.add_argument("--sku", help="商品价格表卖家SKU")
    g.add_argument("--name", help="商品价格表产品中文名包含")
    g.add_argument("--prefix", help="商品价格表 vendorCode 前缀码")
    g.add_argument("--vc", help="vendorCode 列表（逗号分隔）")
    g.add_argument("--all", action="store_true", help="全部映射商品（默认）")
    p.add_argument("--shops", help="限定店铺ID（逗号分隔，默认全部已 fetch 店铺）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--yes", action="store_true", help="跳过不可逆操作确认")

    if with_price:
        p.add_argument("--price", type=int, help="目标价（默认 floor(商品价格表实际售价)）")
        p.add_argument("--discount", type=int, help="折扣 0-100（不传=不改）")
        p.add_argument("--club-discount", type=int, help="club折扣 0-100（不传=不改）")
        p.add_argument("--keep-price", action="store_true", help="价格保持当前值（只改折扣/俱乐部折扣）")
        p.add_argument("--auto-review", action="store_true",
                       help="改价后自动「应用新价格」（降价 30-49.9%% 进审查时，精确匹配刚改价商品）")
    if with_stock:
        p.add_argument("--amount", type=int, default=0, help="目标库存（默认 0）")


def parse_shops(text: Any) -> Any:
    """把 --shops 的 'a,b' 文本解析为 [int]；空/非法返回 None 或抛 ValueError。"""
    raw = (text or "").strip() if isinstance(text, str) else text
    if not raw:
        return None
    return [int(x.strip()) for x in str(raw).split(",") if x.strip()]


from sellerops.settings import paths as config

def _add_ops_args(p, *, with_price=False, with_stock=False):
        # 唯一实现下沉到 framework.cli_args（argparse-only，保证本模块启动零业务依赖）
        add_ops_args(p, with_price=with_price, with_stock=with_stock)

def _add_discount_wb_args(parser):
        parser.add_argument("--apply", action="store_true", help="真正提交修改（默认 dry-run 预览）")
        parser.add_argument("--threshold", type=int, default=None,
                            help=f"折扣阈值：只处理折扣>该值的商品（默认 {config.DISCOUNT_THRESHOLD_DEF}；指定 --all 或指定 --vc 时默认不限阈值）")
        parser.add_argument("--all", action="store_true",
                            help="不限折扣阈值，处理指定条件下的所有在架商品（等价于 --threshold -1）")
        parser.add_argument("--below", type=int, default=-1,
                            help="额外命中折扣 <N 的商品（与 --threshold 取并集，走 WB 折扣升序列表接口；例 --threshold 55 --below 40 = 折扣>55%% 或 <40%%；-1=不启用）")
        parser.add_argument("--target", type=int, default=config.DISCOUNT_TARGET_DEF,
                            help=f"目标折扣（默认 {config.DISCOUNT_TARGET_DEF}）")
        parser.add_argument("--name", default="", help="商品价格表产品中文名包含匹配（如 笔记本电脑）")
        parser.add_argument("--vc", default="", help="指定单个或多个 vendorCode（逗号分隔）")
        parser.add_argument("--prefix", default="", help="vendorCode 4位前缀码包含匹配（如 DSGI）")
        parser.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部凭证店铺）")
        parser.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
        parser.add_argument("--chunk", type=int, default=100, help="每批提交数量（默认 100，最大 300）")


def _add_shelve_args(parser):
        parser.add_argument("nms", nargs="*", help="WB 商品码列表（支持空格或逗号分隔，如 248364237 388854754）")
        parser.add_argument("--nm", default="", help="WB 商品码（逗号分隔多个，兼容命令行参数传参）")
        parser.add_argument("--vc", default="", help="完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）")
        parser.add_argument("--prefix", default="", help="4位前缀码（如 ABCD 或 BCS-ABCD）")
        parser.add_argument("--price", type=float, default=None, help="指定售价（CNY），不填时自动从本地价格映射表或商品价格表推导")
        parser.add_argument("--dims", default="", help="自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）")
        parser.add_argument("--length", type=int, default=None, help="包装长 (cm)")
        parser.add_argument("--width", type=int, default=None, help="包装宽 (cm)")
        parser.add_argument("--height", type=int, default=None, help="包装高 (cm)")
        parser.add_argument("--weight", type=float, default=None, help="包装毛重 (kg)")
        parser.add_argument("--shops", default="", help="目标店铺 ID（逗号分隔，如 9352,9353；默认上架到全部活跃店铺）")
        parser.add_argument("--stock", type=int, default=999, help="上架库存量（默认 999）")
        parser.add_argument("--cn", default="", help="商品中文名（辅助匹配价格与包装规格）")
        parser.add_argument("--file", default="", help="批量上架文件路径（支持 .txt 每行一个商品码，或 .json 商品列表）")
        parser.add_argument("--cn-stock", default="", help="按中文名指定库存（'中文名:库存,...'）")
        parser.add_argument("--interval", type=float, default=1.0, help="批次/商品请求间隔秒（默认 1.0）")
        parser.add_argument("--apply", action="store_true", help="真正调用平台接口执行（默认仅 dry-run 预览）")

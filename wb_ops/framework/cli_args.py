# -*- coding: utf-8 -*-
"""
wb_ops CLI 参数定义公共件（argparse-only，无任何业务依赖）

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
    p.add_argument("--sync", action="store_true",
                   help="执行后自动同步在架商品并合并映射表（默认不自动同步/不写后验证，仅打印提示）")
    if with_price:
        p.add_argument("--price", type=int, help="目标价（默认 floor(商品价格表双倍售价)）")
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

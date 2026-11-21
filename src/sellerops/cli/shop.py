"""命令定义：shop。"""
import argparse
from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('list', help="账号店铺列表")
    p.add_argument("--name", help="精确查找店名，重名或未命中时列出候选")
    bind(p, 'sellerops.cli.catalog_handlers', 'run_shops')

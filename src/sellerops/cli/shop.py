"""店铺查阅及软归档。"""
import argparse

from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('list', help='查看本地店铺状态和快照时间')
    p.add_argument('--name', help='精确查找店名')
    p.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    bind(p, 'sellerops.features.catalog.shops', 'run_list')

    for action, title in (('archive', '将店铺软归档'), ('restore', '恢复店铺')):
        p = sub.add_parser(action, help=title)
        p.add_argument('--shop-id', required=True, type=int, help='店铺 ID')
        p.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
        bind(p, 'sellerops.features.catalog.shops', 'run_status')

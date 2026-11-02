"""Seller operations command line entry point."""
import argparse
import sys

from sellerops.cli import catalog, listings, orders, pricing, support, system
from sellerops.cli.binding import dispatch
from sellerops.core.utils import ensure_utf8_stdout
from sellerops.settings import paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='sellerops', description='多平台卖家运营工具')
    parser.add_argument('--data-dir', help='数据与凭证目录（默认项目根目录 data/；可用 SELLEROPS_DATA_DIR 覆盖）')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for group in (catalog, listings, pricing, orders, support, system):
        group.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    # Select paths before registering arguments whose defaults are data-file paths.
    location_parser = argparse.ArgumentParser(add_help=False)
    location_parser.add_argument('--data-dir')
    location, _ = location_parser.parse_known_args(argv)
    paths.configure(location.data_dir)
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        print('\n已中断', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'[错误] {exc}', file=sys.stderr)
        return 1

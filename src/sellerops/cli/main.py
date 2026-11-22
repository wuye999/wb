"""Seller operations command line entry point."""
import argparse
import sys
from threading import Lock

from sellerops.cli import catalog, listing, login, marketing, order, product, report, shop, support, task
from sellerops.cli.binding import bind, dispatch
from sellerops.core.utils import ensure_utf8_stdout
from sellerops.settings import paths


class CommandParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = CommandParser(prog='sellerops', description='多平台卖家运营工具')
    parser.add_argument('--data-dir', help='数据与凭证目录（默认项目根目录 data/；可用 SELLEROPS_DATA_DIR 覆盖）')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for commands, description in ((shop, "店铺"), (catalog, "快照与映射"), (product, "商品运营"),
                                  (listing, "商品上架"), (marketing, "营销"), (order, "订单履约"),
                                  (report, "统计报告"), (support, "客服"), (task, "自动化")):
        group = sub.add_parser(commands.__name__.split(".")[-1], help=description)
        commands.register(group.add_subparsers(dest="action", required=True))
    login.register(sub)
    migration = sub.add_parser('migrate-legacy', help='离线迁移旧商品资料、快照和提交记录')
    bind(migration, 'sellerops.persistence.migrate_legacy', 'run')
    return parser


def leaf_commands(parser):
    """按显示顺序遍历可执行命令，供离线检查及参考文档复用。"""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                if child.get_default("handler"):
                    yield name, child
                else:
                    for suffix, leaf in leaf_commands(child):
                        yield name + " " + suffix, leaf


_command_lock = Lock()


def main(argv: list[str] | None = None) -> int:
    if not _command_lock.acquire(blocking=False):
        print("本进程已有命令运行。请另启一条命令处理其他数据目录。", file=sys.stderr)
        return 1
    try:
        ensure_utf8_stdout()
        # Select paths before registering arguments whose defaults are data-file paths.
        location_parser = CommandParser(add_help=False)
        location_parser.add_argument('--data-dir')
        location, _ = location_parser.parse_known_args(argv)
        paths.configure(location.data_dir)
        args = build_parser().parse_args(argv)
        return dispatch(args)
    except KeyboardInterrupt:
        print('\n已中断', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'[错误] {exc}', file=sys.stderr)
        return 1
    finally:
        _command_lock.release()

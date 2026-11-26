"""Seller operations command line entry point."""
import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
from threading import Lock

from sellerops.cli import catalog, listing, login, map as vc_map, marketing, ops, order, product, report, shop, support, task
from sellerops.cli.binding import bind, dispatch
from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError, ValidationError
from sellerops.core.json_values import json_value
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
    group = sub.add_parser('map', help='商品归属')
    vc_map.register(group.add_subparsers(dest='action', required=True))
    doctor = sub.add_parser('doctor', help='快速体检')
    doctor.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    doctor.add_argument('--check-online', action='store_true', help='显式进行只读鉴权')
    doctor.add_argument('--check-updates', action='store_true', help='跳过缓存，立即检查 GitHub 仓库更新')
    bind(doctor, 'sellerops.features.system.doctor', 'run')
    sync = sub.add_parser('sync', help='同步商品快照')
    catalog.add_fetch_args(sync)
    planned = sub.add_parser('ops', help='预览商品操作并保存固定计划单')
    ops.register(planned.add_subparsers(dest='action', required=True))
    ops.register_apply(sub)
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


def execute(args):
    from sellerops.features.products import operation_plan

    action = getattr(args, 'action', None)
    if args.cmd == 'report' and (output := getattr(args, 'out', '')):
        if Path(output).resolve().name.casefold() == Path(paths.current.price_book_xlsx).name.casefold():
            raise ValueError('《商品价格表.xlsx》只读，请为报表另选输出文件')
    if (args.cmd == 'ops'
            or (args.cmd == 'product' and action in operation_plan.PRODUCT_ACTIONS)
            or (args.cmd == 'listing' and action in operation_plan.PUBLISH_ACTIONS)):
        return operation_plan.preview(args, dispatch)
    if args.cmd == 'product' and action == 'banned' and args.apply:
        raise ValueError('请先通过 ops trash 或 ops price 列出目标并生成计划单')
    return dispatch(args)


_command_lock = Lock()


def extract_data_dir(argv: list[str]) -> tuple[str | None, list[str]]:
    """Extract the global data directory wherever it appears before a literal --."""
    location_parser = CommandParser(add_help=False)
    location_parser.add_argument('--data-dir')
    location, remaining = location_parser.parse_known_args(argv)
    return location.data_dir, remaining


def parse_command(argv: list[str]):
    # Select paths before registering arguments whose defaults are data-file paths.
    data_dir, remaining = extract_data_dir(argv)
    paths.configure(data_dir)
    args = build_parser().parse_args(remaining)
    args.data_dir = data_dir
    args.output, args.summary, args.next_action, args.plan_id = {}, '', '', None
    return args


def print_reply(code: int, *, summary: str, plan_id=None, data=None, next_action: str = '') -> int:
    reply = {'ok': code == 0, 'summary': summary, 'plan_id': plan_id,
             'data': data if data is not None else {}, 'next_action': next_action}
    try:
        output = json.dumps(reply, ensure_ascii=False, allow_nan=False, default=json_value)
    except (TypeError, ValueError):
        code = 1
        reply.update(ok=False, summary='结果含无法输出的字段', data={},
                     next_action='查看命令日志和已保存的回执。请勿重发已提交的计划单')
        output = json.dumps(reply, ensure_ascii=False)
    print(output)
    return code


def syntax_message(output: str) -> str:
    message = output.strip().rsplit('error: ', 1)[-1].splitlines()[-1] if output.strip() else ''
    for old, translated in (
            ('the following arguments are required:', '缺少必填参数'),
            ('expected one argument', '需要一个参数值'),
            ('unrecognized arguments:', '无法识别参数'),
            ('invalid choice:', '不支持的选项'),
            ('invalid int value:', '需要整数，收到'),
            ('invalid float value:', '需要数字，收到'),
            ('not allowed with argument', '不能同时使用参数'),
            ('(choose from', '（可选'),
            ('argument ', '参数 ')):
        message = message.replace(old, translated)
    return f'命令参数有误。{message}' if message else '命令参数有误'


def run_json(argv: list[str]) -> int:
    help_text, parser_error = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(help_text), redirect_stderr(parser_error):
            args = parse_command(argv)
    except SystemExit as error:
        if error.code == 0:
            return print_reply(0, summary='已列出命令用法', data={'help': help_text.getvalue()})
        return print_reply(2, summary=syntax_message(parser_error.getvalue()),
                           next_action='查看对应命令的 --help，补齐或修正参数')
    try:
        with redirect_stdout(sys.stderr):
            code = execute(args)
    except KeyboardInterrupt:
        code, args.summary = 130, '操作已中断'
    except AuthenticationError:
        code, args.summary = 1, '平台鉴权失败，请核对本机凭证和店铺权限'
        args.next_action = '可用 doctor --check-online --json 核对鉴权。提交中的计划请先查看状态'
    except (NetworkError, RateLimitError):
        code, args.summary = 1, '平台连接异常或请求受限'
    except (ValueError, ValidationError) as error:
        code, args.summary = 1, f'请核对输入或数据。{error}'
    except OSError:
        code, args.summary = 1, '本机文件无法读取或写入'
        args.next_action = '核对文件是否存在、是否被占用以及目录权限'
    except Exception as error:
        print(f'[错误] {type(error).__name__}', file=sys.stderr)
        code, args.summary = 1, '命令执行失败'
    if code and not args.next_action:
        args.next_action = ('核对计划单号和回执，提交中或结果未知的请求请勿重发'
                            if args.cmd == 'apply' else
                            '查看上方错误和 stderr 日志，核对命令参数或运行 doctor --json')
    return print_reply(code, summary=args.summary or ('已完成' if code == 0 else '命令未完成'),
                       plan_id=args.plan_id, data=args.output, next_action=args.next_action)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    options = arguments[:arguments.index('--')] if '--' in arguments else arguments
    json_mode = '--json' in options
    ensure_utf8_stdout()
    if not _command_lock.acquire(blocking=False):
        summary = '本进程已有命令运行'
        next_action = '请另启一条命令处理其他数据目录'
        if json_mode:
            return print_reply(1, summary=summary, next_action=next_action)
        print(f'{summary}。{next_action}。', file=sys.stderr)
        return 1
    try:
        if json_mode:
            return run_json(arguments)
        return execute(parse_command(arguments))
    except KeyboardInterrupt:
        if json_mode:
            return print_reply(130, summary='操作已中断', next_action='提交中的计划请先查看状态')
        print('\n已中断', file=sys.stderr)
        return 130
    except Exception as error:
        if json_mode:
            return print_reply(1, summary='无法启动命令', next_action='核对 --data-dir 或运行 doctor --json')
        print(f'[错误] {error}', file=sys.stderr)
        return 1
    finally:
        _command_lock.release()

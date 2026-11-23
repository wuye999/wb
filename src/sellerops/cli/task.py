"""命令定义：task。"""
import argparse
from sellerops.cli.binding import bind
from sellerops.cli.arguments import add_shop_scope, shop_ids


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('天数必须为正整数') from None
    if days < 1:
        raise argparse.ArgumentTypeError('天数必须为正整数')
    return days


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('run', help="每日任务（morning=促销报名+生成折扣计划 / adjust-discounts=只生成折扣计划）")
    p.add_argument("mode", choices=["morning", "adjust-discounts"])
    p.add_argument("--apply", action="store_true", help="运行每日流程（默认只列步骤）；折扣计划仍需确认后用 apply --plan 提交")
    add_shop_scope(p)
    bind(p, 'sellerops.features.system.daily', 'run_daily_cmd')

    p = sub.add_parser('schedule', help="创建/删除 Windows 计划任务（默认预览，--apply 执行）")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--shops", type=shop_ids, help="注册在这些店铺运行的任务")
    scope.add_argument("--all-shops", action="store_true", help="注册覆盖全部配置店铺的任务")
    p.add_argument("--remove", action="store_true", help="删除全部任务")
    p.add_argument("--apply", action="store_true", help="真正注册或删除任务")
    p.add_argument("--yes", action="store_true", help="确认删除任务")
    bind(p, 'sellerops.features.system.schedule', 'run')

    p = sub.add_parser('clean', help='按保留天数清理日志、交付物和临时目录（默认预览）')
    p.add_argument('--days', type=_positive_days, default=20, help='保留最近 N 天，至少 1 天（默认 20）')
    p.add_argument('--apply', action='store_true', help='删除过期文件和空子目录')
    p.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    bind(p, 'sellerops.features.system.clean', 'run')

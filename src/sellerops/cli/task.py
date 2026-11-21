"""命令定义：task。"""
import argparse
from sellerops.cli.binding import bind
from sellerops.cli.arguments import add_shop_scope, shop_ids


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('run', help="每日任务（morning=报名+改价 / adjust-discounts=只改价）")
    p.add_argument("mode", choices=["morning", "adjust-discounts"])
    p.add_argument("--apply", action="store_true", help="执行每日流程（默认预览）")
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

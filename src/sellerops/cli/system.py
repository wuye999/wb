"""system 命令定义。"""
import argparse

from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser('login', help='安装专用浏览器或连接 BCS、马帮、WB')
    p.add_argument('platform', choices=['setup', 'bcs', 'mabang', 'wb'])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--resume', action='store_true', help='仅检查专用浏览器中已保存的登录态')
    mode.add_argument('--keep-open', action='store_true', help='等待网页登录成功后，在同一个窗口完成连接')
    p.add_argument('--bind', action='append', default=[], metavar='BCS_ID=WB_UUID',
                   help='仅 WB，确认一组新店对应关系，可重复指定')
    bind(p, 'sellerops.features.login', 'run')

    p = sub.add_parser("daily", help="每日任务（morning=报名+改价 / check=只改价）")
    p.add_argument("mode", choices=["morning", "check"])
    p.add_argument("--apply", action="store_true", help="执行每日流程（默认预览）")
    p.add_argument("--shops", default="", help="限定店铺 ID")
    bind(p, 'sellerops.automation.daily', 'run_daily_cmd')

    p = sub.add_parser("schedule", help="创建/删除 Windows 计划任务（默认预览，--apply 执行）")
    p.add_argument("--remove", action="store_true", help="删除全部任务")
    p.add_argument("--apply", action="store_true", help="真正注册或删除任务")
    p.add_argument("--yes", action="store_true", help="确认删除任务")
    bind(p, 'sellerops.automation.schedule', 'run')

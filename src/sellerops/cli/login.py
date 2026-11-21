"""命令定义：login。"""
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
    bind(p, 'sellerops.features.system.login', 'run')

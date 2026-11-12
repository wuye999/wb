"""system 命令定义。"""
import argparse

from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("mabang-login", help="登录马帮或导入人工 HAR，更新本地会话与 API 凭证")
    p.add_argument("--from-har", metavar="PATH", help="导入成功登录和预报列表 HAR（相对路径从数据目录 har/ 读取）")
    bind(p, 'sellerops.features.orders.mabang_login', 'run')

    p = sub.add_parser("cookies-update", help="用一店人工抓包为同一 WB 登录下的已配置店铺重签凭证")
    p.add_argument("md_file", help="含单个 fetch 块的文件（相对路径从数据目录 har/ 读取，或传绝对路径）")
    bind(p, 'sellerops.integrations.sessions.capture', 'run_cookies_update')

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

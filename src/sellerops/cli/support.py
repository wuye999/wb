"""support 命令定义。"""
import argparse
from sellerops.settings import paths as config
from sellerops.cli.binding import bind


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("questions", help="买家未处理提问查询与回复")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--reply", default="", help="回复内容（配合 --question-id 或 --reply-all）")
    p.add_argument("--apply", action="store_true", help="真正提交回复（默认预览）")
    p.add_argument("--question-id", default="", help="回复指定提问 ID")
    p.add_argument("--reply-all", action="store_true", help="回复本店全部未处理提问（需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认回复全部（公开发言）")
    p.add_argument("--no-detail", action="store_true", help="跳过 WB 商品详情拉取（只显示本地中文名，速度快）")
    bind(p, 'sellerops.features.support.questions', 'run')

    p = sub.add_parser("questions-watch", help="买家提问实时监听（front=前台AI打印提问/商品信息+手动回复，back=后台AI LLM 自动回复）")
    p.add_argument("--interval", type=int, default=0, help="轮询间隔秒（默认 90，或 credentials 的 ai.watch_interval）")
    p.add_argument("--mode", choices=["front", "back"], default="front",
                       help="front=展示提问/商品信息；back=LLM 生成草稿，提交仍需 --apply")
    p.add_argument("--apply", action="store_true", help="生成并提交回复（同时选择 back；需配 ai.api_key）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--once", action="store_true", help="只跑一轮就退出（测试用）")
    bind(p, 'sellerops.features.support.questions_watch', 'run')

    p = sub.add_parser("chat-watch", help="AI 售后客服：买家聊天实时监听（front=前台AI展示/手动，back=后台AI自动回复；默认只展示）")
    p.add_argument("--interval", type=int, default=0, help="轮询间隔秒（默认取 credentials 的 ai.watch_interval，90）")
    p.add_argument("--mode", choices=["front", "back"], default="front",
                       help="front=展示买家消息/商品信息；back=LLM 生成草稿，发送仍需 --apply")
    p.add_argument("--apply", action="store_true", help="生成并发送回复（同时选择 back；需配 ai.api_key）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--once", action="store_true", help="只跑一轮就退出（测试用）")
    p.add_argument("--limit", type=int, default=20, help="每店最多取 N 个会话（默认 20）")
    p.add_argument("--history-limit", type=int, default=50, dest="history_limit", help="会话历史拉取条数（默认 50）")
    p.add_argument("--since-hours", type=int, default=72, dest="since_hours",
                       help="只处理最近 N 小时内的买家消息（默认 72；0=不限，会补回历史旧账）")
    p.add_argument("--max-turns", type=int, default=3, dest="max_turns", help="拼进提示词的最近对话轮数（默认 3）")
    bind(p, 'sellerops.features.support.chat_watch', 'run')

    p = sub.add_parser("chats", help="查询 WB 买家聊天会话与消息历史（只读，用于验证/排障）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=20, help="每店最多取 N 个会话（默认 20）")
    p.add_argument("--chat-id", default="", dest="chat_id", help="只看指定会话 chatID")
    p.add_argument("--history", type=int, default=0,
                       help="展示最近 N 条消息历史（默认仅在指定 --chat-id 时展示 10 条）")
    p.add_argument("--since-hours", type=int, default=0, dest="since_hours",
                       help="只列出最新消息在最近 N 小时内的会话（0=不限，默认）")
    p.add_argument("--no-detail", action="store_true", dest="no_detail",
                       help="跳过 WB 商品详情拉取（只显示本地中文名，速度快）")
    bind(p, 'sellerops.features.support.chat_inspect', 'run')

    p = sub.add_parser("appeals", help="查询 WB 平台投诉单（只读；未处理/剩余天数筛选，输出去重商品编号与供应商代码）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）")
    p.add_argument("--days", type=int, default=0,
                       help="仅命中剩余天数恰好=N 的未处理投诉（decide_counter，0=不筛选，默认）")
    p.add_argument("--days-max", type=int, default=0, dest="days_max",
                       help="仅命中剩余天数≤N 的未处理投诉（0=不筛选，默认；与 --days 同给时 --days 优先）")
    p.add_argument("--type", default="in", choices=["in", "out"], help="投诉方向：in=发往本店（默认）/ out=本店发出")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取 N 条投诉列表（0=不限，翻页到底）")
    p.add_argument("--no-cn", action="store_true",
                       help="不解析商品中文名（供应商代码仍会解析；本地真源查不到的商品标注「本地真源未收录」）")
    bind(p, 'sellerops.features.support.complaints', 'run')

    p = sub.add_parser("ratings",
                           help="查询商品链接评分与评价情况（只读；支持按评分/店铺/1星评论筛选，导出包含价格表数据的报表）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部）")
    p.add_argument("--rating", type=float, default=None, help="精确筛选指定评分（如 1 或 1.0）")
    p.add_argument("--min-rating", type=float, default=None, help="最低评分（>=）")
    p.add_argument("--max-rating", type=float, default=None, help="最高评分（<=，如 1 或 2 筛选低分商品）")
    p.add_argument("--one-star", action="store_true", help="筛选存在 1 星评论的商品（oneStar >= 1）")
    p.add_argument("--two-star", action="store_true", help="筛选存在 2 星评论的商品（twoStar >= 1）")
    p.add_argument("--negative", "--bad", action="store_true", dest="negative",
                       help="筛选存在差评的商品（即 1 星或 2 星差评数 >= 1）")
    p.add_argument("--stars", default="", help="指定星级评论筛选（逗号分隔，如 1,2 筛选含 1 星或 2 星差评的商品）")
    p.add_argument("--min-one-star", type=int, default=0, help="筛选 1 星评论数 >= N 的商品")
    p.add_argument("--min-feedbacks", type=int, default=1, help="最低评价总数（默认 1，仅看有评价商品；设为 0 含无评价）")
    p.add_argument("--days", type=int, default=365, help="统计天数跨度（默认 365 天）")
    p.add_argument("--name", default="", help="按产品中文名包含筛选")
    p.add_argument("--prefix", default="", help="按 4 位前缀码筛选")
    p.add_argument("--vc", default="", help="按 vendorCode 筛选")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取商品条数（0=不限）")
    p.add_argument("--out", default="", help="自定义导出文件路径（支持 .csv / .xlsx）")
    bind(p, 'sellerops.features.support.ratings', 'run')

    p = sub.add_parser("ai-test", help="使用本地问答集调用 LLM 对照生成回复（会联网）")
    p.add_argument("--qa", default=config.AI_TEST_QA, help="测试数据集 json（默认 data/ai_test_qa.json）")
    bind(p, 'sellerops.features.support.ai_reply_test', 'run')

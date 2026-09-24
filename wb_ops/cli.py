# -*- coding: utf-8 -*-
"""
wb_ops 统一 CLI 入口（聚合「检查价格」+「促销折扣」全部功能）

用法：python wb.py <子命令>（或 python -m wb_ops <子命令>）
子命令见 docs/CLI.md；每个子命令的实现在对应业务模块中，本文件只做解析与分发。
"""
import argparse
import sys

from wb_ops import common
from wb_ops import config
from wb_ops.framework import cli_args

def build_parser():
    ap = argparse.ArgumentParser(prog="wb", description="Wildberries/BCS 卖家自动化统一入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("shops", help="账号店铺列表")

    p = sub.add_parser("fetch", help="拉取店铺商品数据（自动先同步 WB）")
    p.add_argument("--shop-id", type=int, default=None, help="店铺ID（默认全部）")
    p.add_argument("--no-sync", action="store_true", help="跳过 BCS 同步，直接拉取上次数据")

    p = sub.add_parser("mapping", help="生成统一核对工作台（全部活跃店铺并集，一页两区）")
    p.add_argument("--legacy", action="store_true", help="旧模式：仅主店候选")

    p = sub.add_parser("mapping-import", help="导入核对结果 → 生成映射表（旧格式）")
    p.add_argument("file", help="核对结果 JSON")

    p = sub.add_parser("mapping-check", help="映射表核查工作台（带图，核对匹配是否有误）")
    p.add_argument("--tol", type=int, default=5, help="价格偏差阈值（元，默认 5）")

    p = sub.add_parser("mismatch-check", help="货不对板筛查工作台（看图勾选，导出 vc 下架）")
    p.add_argument("--cn", default="", help="只渲染指定中文名（精确匹配，如『牙膏-紫色』）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD（按映射表创建时间/上架时间筛选）")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（缺省=今天）")
    p.add_argument("--days", type=int, default=0, help="最近 N 天（含今天；与 --begin/--end 冲突时以具体日期为准）")

    sub.add_parser("review", help="多店铺待审核工作台")

    p = sub.add_parser("merge", aliases=["mapping-merge"], help="增量合并审核 → 映射表（file 可选）")
    p.add_argument("file", nargs="?", default=None, help="本次审核结果 JSON（可选）")

    p = sub.add_parser("mapping-rename", help="纠偏/修改商品中文名（自动级联更新全部店铺单表与聚合总表）")
    p.add_argument("--vc", default="", help="要改名的 vendorCode")
    p.add_argument("--cn", default="", help="新产品中文名")
    p.add_argument("--reason", default="人工纠偏", help="改名原因说明")
    p.add_argument("--file", default="", help="批量改名 JSON 文件路径（含 [{'vc': ..., 'cn': ...}]）")

    p = sub.add_parser("shops-mapping", aliases=["mapping-sync"], help="刷新/生成各店铺独立映射表（data/shops/shop_*.xlsx）")
    p.add_argument("--shop-id", type=int, default=None, help="指定店铺ID（默认全部活跃店铺）")
    p.add_argument("--force", action="store_true", help="强制全量重新构建")

    def _add_ops_args(p, *, with_price=False, with_stock=False):
        # 唯一实现下沉到 framework.cli_args（argparse-only，保证本模块启动零业务依赖）
        cli_args.add_ops_args(p, with_price=with_price, with_stock=with_stock)

    p = sub.add_parser("price", help="改价/改折扣（默认 WB 原生 dp-api 批量，自动确认降价/隔离区弹窗；dry-run 默认）")
    _add_ops_args(p, with_price=True)
    p.add_argument("--chunk", type=int, default=100, help="每批提交条数（≤300，对齐 discount 实践，默认 100）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")

    p = sub.add_parser("price-wb", help="[别名] 与 price 相同（WB 原生 dp-api 批量，显式点名通道）")
    _add_ops_args(p, with_price=True)
    p.add_argument("--chunk", type=int, default=100, help="每批提交条数（≤300，对齐 discount 实践，默认 100）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")

    p = sub.add_parser("price-bcs", help="[备选] 改价走 BCS 接口（price/batch，WB 原生不可用时的兜底）")
    _add_ops_args(p, with_price=True)

    p = sub.add_parser("stock", help="改库存（默认 WB 原生在线接口；dry-run 默认，归零须 --yes）")
    _add_ops_args(p, with_stock=True)
    p.add_argument("--chunk", type=int, default=500, help="每批提交条数（≤1000，探针实测 1000 可行，默认 500）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")
    p.add_argument("--max-pages", type=int, default=200, help="WB 游标分页安全上限（--resolve live 时使用）")
    p.add_argument("--resolve", choices=["snapshot", "live"], default="snapshot",
                   help="chrtId 解析源：snapshot=BCS 快照（默认，零请求）；live=WB portal/stocks 实时拉取（绕过快照滞后）")

    p = sub.add_parser("stock-wb", help="[别名] 与 stock 相同（WB 原生在线接口，显式点名通道）")
    _add_ops_args(p, with_stock=True)
    p.add_argument("--chunk", type=int, default=500, help="每批提交条数（≤1000，探针实测 1000 可行，默认 500）")
    p.add_argument("--interval", type=float, default=0.3, help="批/店间请求间隔秒")
    p.add_argument("--max-pages", type=int, default=200, help="WB 游标分页安全上限（--resolve live 时使用）")
    p.add_argument("--resolve", choices=["snapshot", "live"], default="snapshot",
                   help="chrtId 解析源：snapshot=BCS 快照（默认，零请求）；live=WB portal/stocks 实时拉取（绕过快照滞后）")

    p = sub.add_parser("stock-bcs", help="[备选] 改库存走 BCS 接口（stock/batchSetByChrtIdsBatch，WB 原生不可用时的兜底）")
    _add_ops_args(p, with_stock=True)

    p = sub.add_parser("trash", help="下架（移回收站，不可逆）")
    _add_ops_args(p)

    p = sub.add_parser("replicate", help="跨店复制上架：把部分覆盖的商品上架到缺失店铺（dry-run 默认）")
    p.add_argument("--vc", default="", help="指定 vendorCode（逗号分隔）")
    p.add_argument("--prefix", default="", help="前缀码筛选（4 位大写）")
    p.add_argument("--name", default="", help="映射表中文名包含匹配")
    p.add_argument("--shops", default="", help="限定目标店铺 id 逗号分隔（默认全部缺失店）")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个 vc（0=不限）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--no-verify", action="store_true", help="跳过执行后 fetch 复核")
    p.add_argument("--sync", action="store_true",
                   help="启动前同步全部店铺 + 上架后复核并合并映射表（默认不自动同步/不写后验证，仅打印提示；用本地快照判断可能滞后，需最新务必加 --sync）")
    p.add_argument("--interval", type=float, default=1.0, help="批次上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")

    p = sub.add_parser("import-shelve", help="他人映射表导入上架：他人有我方无的商品（按真实 WB商品码 匹配）上架到我的店铺")
    p.add_argument("xlsx", help="他人映射表 xlsx 路径（同项目「映射总表」格式）")
    p.add_argument("--cn", default="", help="他人表中文名包含过滤（逗号分隔多个）")
    p.add_argument("--shops", default="", help="限定目标店铺 id 逗号分隔（默认全部店铺）")
    p.add_argument("--limit", type=int, default=0, help="最多处理 N 个商品（0=不限）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--no-verify", action="store_true", help="跳过执行后 fetch 复核")
    p.add_argument("--sync", action="store_true",
                   help="启动前同步全部店铺 + 上架后复核并合并映射表（默认不自动同步/不写后验证，仅打印提示；用本地快照判断可能滞后，需最新务必加 --sync）")
    p.add_argument("--interval", type=float, default=1.0, help="批次上架请求间隔秒")
    p.add_argument("--cn-stock", default="", help="按中文名指定上架库存：'中文名:库存,...'（未指定默认 999）")

    p = sub.add_parser("promo-apply", help="促销报名（cookie 会话，applyAll）")
    p.add_argument("--apply", action="store_true", help="真正报名（默认 dry-run 预览）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--days", type=int, default=60, help="查询结束=N天后")
    p.add_argument("--days-back", type=int, default=90, help="查询起始=N天前")
    p.add_argument("--sleep", type=float, default=1.0, help="活动间请求间隔秒")

    p = sub.add_parser("promo-goods", help="查询 WB 广告推广中被推广的商品（中文名/供应商代码/WB商品码，只读）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）")
    p.add_argument("--status", default="", help="活动状态ID逗号分隔（默认 4,9,11 = 后台默认视图）")
    p.add_argument("--page-size", type=int, default=100, help="活动列表分页大小（默认 100）")
    p.add_argument("--max-pages", type=int, default=50, help="活动列表最多翻页数（安全上限，默认 50）")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取 N 个活动（0=不限，翻页到底）")
    p.add_argument("--no-cn", action="store_true", help="不解析商品中文名（供应商代码仍解析）")

    p = sub.add_parser("promo-gap",
                       help="只读：推广 × 销量双向错配审计（gap=该推没推 / waste=在推但没销量该关）")
    p.add_argument("--mode", choices=["gap", "waste", "both"], default="gap",
                   help="审计方向：gap=该推没推(默认) / waste=该关的(在推广但本店单数<阈值) / both=两个都出")
    p.add_argument("--shops", default="", help="限定店铺，逗号分隔，支持店铺ID或短名（如 9352 或 袁州1；默认全部已填 cookie 店铺）")
    p.add_argument("--min", type=int, default=4, help="单数阈值（默认 4）：gap=单数≥N；waste=单数<N")
    p.add_argument("--days", type=int, default=7, help="近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）")
    p.add_argument("--date", default="", help="单天 YYYY-MM-DD（等价 --begin=--end）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（单独给按单天处理）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）")
    p.add_argument("--status", default="", help="推广活动状态ID逗号分隔（默认 4,9,11 = 含暂停；只算在投用 --status 9）")
    p.add_argument("--listed-only", action="store_true", help="[gap] 只输出「可直接补推」组（默认两组都出）")
    p.add_argument("--top", type=int, default=0, help="每组控制台只显示前 N 名（0=全部；CSV 始终写全量）")
    p.add_argument("--no-cn", action="store_true", help="不补全商品中文名（飞书表内已有的仍显示）")
    p.add_argument("--url", default="", help="飞书表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="飞书表名（默认 订单登记）")
    p.add_argument("--page-size", type=int, default=100, help="推广活动列表分页大小（默认 100）")
    p.add_argument("--max-pages", type=int, default=50, help="推广活动列表最多翻页数（默认 50）")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取 N 个活动（0=不限）")

    # WB 原生批量改折扣参数辅助函数
    def _add_discount_wb_args(parser):
        parser.add_argument("--apply", action="store_true", help="真正提交修改（默认 dry-run 预览）")
        parser.add_argument("--threshold", type=int, default=None,
                            help=f"折扣阈值：只处理折扣>该值的商品（默认 {config.DISCOUNT_THRESHOLD_DEF}；指定 --all 或指定 --vc 时默认不限阈值）")
        parser.add_argument("--all", action="store_true",
                            help="不限折扣阈值，处理指定条件下的所有在架商品（等价于 --threshold -1）")
        parser.add_argument("--below", type=int, default=-1,
                            help="额外命中折扣 <N 的商品（与 --threshold 取并集，走 WB 折扣升序列表接口；例 --threshold 55 --below 40 = 折扣>55%% 或 <40%%；-1=不启用）")
        parser.add_argument("--target", type=int, default=config.DISCOUNT_TARGET_DEF,
                            help=f"目标折扣（默认 {config.DISCOUNT_TARGET_DEF}）")
        parser.add_argument("--name", default="", help="商品价格表产品中文名包含匹配（如 笔记本电脑）")
        parser.add_argument("--vc", default="", help="指定单个或多个 vendorCode（逗号分隔）")
        parser.add_argument("--prefix", default="", help="vendorCode 4位前缀码包含匹配（如 DSGI）")
        parser.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部凭证店铺）")
        parser.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
        parser.add_argument("--chunk", type=int, default=100, help="每批提交数量（默认 100，最大 300）")
        parser.add_argument("--verify", action="store_true", help="执行后验证（默认关闭，因 WB 异步生效延迟）")

    p = sub.add_parser("discount",
                       help="折扣改价（WB 原生批量，从高到低查询，支持 --name/--vc/--shops 灵活筛选，默认不做写后验证）")
    _add_discount_wb_args(p)

    p = sub.add_parser("discount-wb",
                       help="[别名] discount 别名，WB 原生批量改折扣")
    _add_discount_wb_args(p)

    p = sub.add_parser("discount-scan",
                       help="[别名] discount 别名，WB 原生批量改折扣")
    _add_discount_wb_args(p)

    p = sub.add_parser("discount-bcs",
                       help="[旧版] 走 BCS 接口全量改折扣（慢，默认不启用，仅供按需手动调用）")
    p.add_argument("--apply", action="store_true", help="真正提交（默认 dry-run）")
    p.add_argument("--threshold", type=int, default=config.DISCOUNT_THRESHOLD_DEF,
                   help="阈值：只处理折扣>该值的商品；全量设目标值用 --threshold -1")
    p.add_argument("--target", type=int, default=config.DISCOUNT_TARGET_DEF, help="目标折扣")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    p.add_argument("--sync", action="store_true",
                   help="执行前同步 BCS 缓存 + 提交后同步复核（默认不自动同步/不写后验证，仅打印提示）")
    p = sub.add_parser("dimension", help="批量修改尺寸（按价格表或 --dims 自定义，dry-run 默认，--apply 执行）")
    p.add_argument("--vc", default="", help="指定单个或多个 vendorCode（逗号分隔）")
    p.add_argument("--prefix", default="", help="vendorCode 4位前缀码包含匹配")
    p.add_argument("--name", default="", help="商品价格表产品中文名包含匹配")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    p.add_argument("--dims", default="", help="自定义尺寸 '长*宽*高/毛重'（例: 8*14*26/0.3）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--sync", action="store_true",
                   help="执行后同步并合并映射表（默认不自动同步/不写后验证，仅打印提示）")

    p = sub.add_parser("dims-check", help="查询 WB 包装尺寸/重量偏差待验证商品（只读）")
    p.add_argument("--type", choices=["dims", "weight", "all"], default="dims",
                   help="dims=尺寸偏差 / weight=重量偏差 / all=两者合并去重（默认 dims）")
    p.add_argument("--name", default="", help="映射表中文名包含过滤")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多查询 N 条（0=不限）")

    p = sub.add_parser("banned", help="查询并删除被阻止的商品（dry-run 默认，--apply 移到回收站）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    p.add_argument("--yes", action="store_true", help="跳过不可逆确认（移到回收站）")
    p.add_argument("--no-verify", action="store_true", help="跳过执行后验证")

    p = sub.add_parser("clean", help="清理草稿箱/回收站")
    p.add_argument("--target", required=True, choices=["basket", "draft", "all"],
                   help="basket=回收站 / draft=草稿箱 / all=先草稿后回收站")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条")
    p.add_argument("--sync", action="store_true",
                   help="清理前同步 + 清理后自动合并映射表（默认不自动同步/不写后验证，仅打印提示）")

    p = sub.add_parser("price-review", help="价格审核：查看并应用新价格（降价 30-49.9%% 进审查的商品）")
    p.add_argument("--apply", action="store_true", help="真正应用新价格（默认 dry-run）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="每店最多审核 N 个（0=全部）")

    p = sub.add_parser("orders", help="订单查询（自动同步 + 查询指定日期区间）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    p.add_argument("--days", type=int, default=0, help="查询最近 N 天（--begin/--end 未指定时，默认 1 天）")
    p.add_argument("--no-sync", action="store_true", help="跳过订单同步，直接查缓存")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部）")
    p.add_argument("--page-size", type=int, default=50, help="分页每页条数")

    p = sub.add_parser("questions", help="买家未处理提问查询与回复")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--reply", default="", help="回复内容（配合 --question-id 或 --reply-all）")
    p.add_argument("--question-id", default="", help="回复指定提问 ID")
    p.add_argument("--reply-all", action="store_true", help="回复本店全部未处理提问（需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认回复全部（公开发言）")
    p.add_argument("--no-detail", action="store_true", help="跳过 WB 商品详情拉取（只显示本地中文名，速度快）")

    p = sub.add_parser("questions-watch", help="买家提问实时监听（front=前台AI打印提问/商品信息+手动回复，back=后台AI LLM 自动回复）")
    p.add_argument("--interval", type=int, default=0, help="轮询间隔秒（默认 90，或 credentials 的 ai.watch_interval）")
    p.add_argument("--mode", choices=["front", "back"], default="front",
                   help="front=前台AI（打印提问/商品信息到控制台和日志，前台手动回复，不自动提交）；back=后台AI（LLM 自动生成并提交）")
    p.add_argument("--apply", action="store_true", help="等价 --mode back（后台AI自动提交，需配 ai.api_key）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔")
    p.add_argument("--once", action="store_true", help="只跑一轮就退出（测试用）")

    p = sub.add_parser("appeals", help="查询 WB 平台投诉单（只读；未处理/剩余天数筛选，输出去重商品编号与供应商代码）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部已填 cookie 店铺）")
    p.add_argument("--days", type=int, default=0,
                   help="仅命中剩余天数恰好=N 的未处理投诉（decide_counter，0=不筛选，默认）")
    p.add_argument("--type", default="in", choices=["in", "out"], help="投诉方向：in=发往本店（默认）/ out=本店发出")
    p.add_argument("--limit", type=int, default=0, help="每店最多拉取 N 条投诉列表（0=不限，翻页到底）")
    p.add_argument("--no-cn", action="store_true",
                   help="不解析商品中文名（供应商代码仍会解析；本地真源查不到的商品标注「本地真源未收录」）")

    p = sub.add_parser("ai-test", help="离线用 data/ai_test_qa.json 对照测试 AI 客服回复（不联网）")
    p.add_argument("--qa", default=config.AI_TEST_QA, help="测试数据集 json（默认 data/ai_test_qa.json）")

    p = sub.add_parser("mabang-orders", help="马帮待处理订单 SKU 匹配核对/更换（VC→映射表中文名→价格表库存SKU）")
    p.add_argument("--days", type=int, default=30, help="查询最近 N 天订单（默认 30）")
    p.add_argument("--page-size", type=int, default=100, help="订单列表每页条数")
    p.add_argument("--apply", action="store_true", help="真正更换错误匹配（默认 dry-run 只出报表）")

    p = sub.add_parser("mabang-forecast",
                       help="马帮预报批次：已匹配商品订单生成预报批次并上传（dry-run 默认；--check 查批次状态）")
    p.add_argument("--days", type=int, default=30, help="查询最近 N 天订单（默认 30）")
    p.add_argument("--page-size", type=int, default=100, help="订单列表每页条数")
    p.add_argument("--apply", action="store_true", help="全链路执行：生成批次→上传→等待→设置交运方式（各步幂等跳过已完成项）")
    p.add_argument("--check", action="store_true", help="只查询预报批次列表与上传结果统计（上传后 5-10 分钟确认用）")
    p.add_argument("--wait", type=int, default=0, help="上传后等待系统更新的秒数（默认取配置 handover_wait_seconds=150）")
    p.add_argument("--upload-waiting", action="store_true", help="把待上传列表(status=1)中历史批次一并补传")

    p = sub.add_parser("feishu-register", help="马帮订单登记到飞书多维表格（按订单编号去重，dry-run 默认）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="表格名（默认 订单登记）")
    p.add_argument("--scope", choices=["latest", "pending", "all"], default="latest",
                   help="latest=最近500条全状态去重只登新增（默认）；pending=待处理订单；all=全部状态订单（补录历史，配合 --date 或 --begin/--end）")
    p.add_argument("--date", default="", help="单天日期 YYYY-MM-DD（--scope all 用）")
    p.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD（--scope all 用）")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD（--scope all 用）")
    p.add_argument("--days", type=int, default=1, help="查询最近 N 天订单（--scope pending 用，默认 1）")
    p.add_argument("--page-size", type=int, default=100, help="订单列表每页条数")
    p.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run 只列出将登记订单）")
    p.add_argument("--no-pending", action="store_true",
                   help="不合并「待处理订单」数据源（默认 scope=latest 会合并 orderalllist + 待处理订单，"
                        "让只匹配但未进预报/上传/交运流程的订单也能登记）")



    p = sub.add_parser("mabang-stock-register",
                       help="拉取马帮全部库存 SKU → 全量重建飞书「马帮库存登记表」（含附件列「图」）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="马帮库存登记表", help="目标表名（默认 马帮库存登记表）")
    p.add_argument("--apply", action="store_true", help="真正全量重建（默认 dry-run 预览）")

    p = sub.add_parser("mabang-stock-daily",
                       help="「马帮库存登记表」日期列管理：默认只建今天列+更新全部已有日期列；--begin/--date 任一显式给出即进入区间模式（删早于该日的旧列+补建区间缺列，--end 需同用）")
    p.add_argument("--url", default="", help="飞书多维表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="马帮库存登记表", help="库存表名（默认 马帮库存登记表）")
    p.add_argument("--orders-table", default="订单登记", help="订单明细表名（默认 订单登记）")
    p.add_argument("--date", default="", help="单天日期 YYYY-MM-DD（默认今天；显式给出即进入区间模式，会删除该日之前的旧日期列）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（显式给出即删除该日之前的旧日期列，并补建至 --end 的缺列）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin/--date 同用，单独使用直接报错）")
    p.add_argument("--apply", action="store_true", help="真正建列并填充（默认 dry-run 预览）")

    p = sub.add_parser("feishu-vc-stats",
                       help="只读：飞书「订单登记」按供应商代码(BCS编号)统计单数并降序（跨店合并；支持店铺/日期过滤）")
    p.add_argument("--days", type=int, default=7, help="近 N 天（含今天，默认 7；仅当未给 --begin/--end/--date 时生效）")
    p.add_argument("--date", default="", help="单天 YYYY-MM-DD（等价 --begin=--end）")
    p.add_argument("--begin", default="", help="区间开始 YYYY-MM-DD（单独给按单天处理）")
    p.add_argument("--end", default="", help="区间结束 YYYY-MM-DD（必须与 --begin 或 --date 同用）")
    p.add_argument("--shops", default="", help="店铺过滤，逗号分隔，支持店铺ID或短名（如 9352 或 袁州1；默认全部）")
    p.add_argument("--top", type=int, default=0, help="控制台只显示前 N 名（0=全部；CSV 始终写全量）")
    p.add_argument("--by-prefix", action="store_true", help="按 vendorCode 的 4 位前缀码聚合（默认完整 vendorCode）")
    p.add_argument("--no-cn", action="store_true", help="不补全商品中文名（表内已有的仍会显示）")
    p.add_argument("--url", default="", help="飞书表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="表名（默认 订单登记）")

    p = sub.add_parser("mabang-process", aliases=["order-pipeline"],
                       help="马帮订单处理一体：匹配商品→预报单→上传（自动发货）→物流交运→飞书登记（登记含只匹配未进预报流程的订单）")
    p.add_argument("--days", type=int, default=1, help="查询最近 N 天待处理订单（默认 1）")
    p.add_argument("--page-size", type=int, default=100, help="订单列表分页大小（默认 100）")
    p.add_argument("--wait", type=int, default=0, help="上传批次后等待秒数（默认 0 不额外等待）")
    p.add_argument("--url", default="", help="飞书表格地址（可选，默认读配置 feishu.base_url）")
    p.add_argument("--table", default="订单登记", help="飞书订单登记表名（默认 订单登记）")
    p.add_argument("--no-pending", action="store_true",
                   help="飞书登记时不合并「待处理订单」数据源（默认合并，覆盖只匹配未进预报/上传/交运的订单）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run 预览）")

    p = sub.add_parser("cookies-update", help="从抓包 md 刷新凭证")
    p.add_argument("md_file", help="含 fetch 块的 md 文件")

    p = sub.add_parser("daily", help="每日任务（morning=报名+改价 / check=只改价）")
    p.add_argument("mode", choices=["morning", "check"])
    p.add_argument("extra", nargs=argparse.REMAINDER, help="透传给子步骤（如 --shops <店铺ID>）")

    p = sub.add_parser("schedule", help="创建/删除 Windows 计划任务（--plan 只预览不执行）")
    p.add_argument("--remove", action="store_true", help="删除全部任务")
    p.add_argument("--plan", action="store_true",
                   help="只打印将要创建/删除的任务定义（不调用 schtasks，只读预览）")

    p = sub.add_parser("remote-wh", help="成都仓库商品永久删除（dry-run 默认，--apply --yes 执行）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部店铺的成都仓）")
    p.add_argument("--limit", type=int, default=0, help="（预留）每店最多处理 N 条，0=全部")
    p.add_argument("--interval", type=float, default=0.3, help="删除请求间隔秒（默认 0.3）")
    p.add_argument("--parallel", type=int, default=1, help="并发店铺数（默认 1=串行；--apply 时有效，店内仍串行）")
    p.add_argument("--apply", action="store_true", help="真正执行删除（不可逆）")
    p.add_argument("--yes", action="store_true", help="确认永久删除（后台运行必备）")

    def _add_shelve_args(parser):
        parser.add_argument("nms", nargs="*", help="WB 商品码列表（支持空格或逗号分隔，如 248364237 388854754）")
        parser.add_argument("--nm", default="", help="WB 商品码（逗号分隔多个，兼容命令行参数传参）")
        parser.add_argument("--vc", default="", help="完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）")
        parser.add_argument("--prefix", default="", help="4位前缀码（如 ABCD 或 BCS-ABCD）")
        parser.add_argument("--price", type=float, default=None, help="指定售价（CNY），不填时自动从本地价格映射表或商品价格表推导")
        parser.add_argument("--dims", default="", help="自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）")
        parser.add_argument("--length", type=int, default=None, help="包装长 (cm)")
        parser.add_argument("--width", type=int, default=None, help="包装宽 (cm)")
        parser.add_argument("--height", type=int, default=None, help="包装高 (cm)")
        parser.add_argument("--weight", type=float, default=None, help="包装毛重 (kg)")
        parser.add_argument("--shops", default="", help="目标店铺 ID（逗号分隔，如 9352,9353；默认上架到全部活跃店铺）")
        parser.add_argument("--stock", type=int, default=999, help="上架库存量（默认 999）")
        parser.add_argument("--cn", default="", help="商品中文名（辅助匹配价格与包装规格）")
        parser.add_argument("--file", default="", help="批量上架文件路径（支持 .txt 每行一个商品码，或 .json 商品列表）")
        parser.add_argument("--cn-stock", default="", help="按中文名指定库存（'中文名:库存,...'）")
        parser.add_argument("--interval", type=float, default=1.0, help="批次/商品请求间隔秒（默认 1.0）")
        parser.add_argument("--apply", action="store_true", help="真正调用平台接口执行（默认仅 dry-run 预览）")
        parser.add_argument("--sync", action="store_true", help="执行完成后触发全量同步并合并映射表")
        parser.add_argument("--no-verify", action="store_true", help="跳过写后验证")

    p = sub.add_parser("shelve", help="新版批量上架：输入 WB 商品码推送到店铺（支持指定前缀/完整VC/价格/尺寸/店铺）")
    _add_shelve_args(p)

    p = sub.add_parser("shelve-old", help="旧版上架建卡：输入 WB 商品码推送到店铺（支持自定义完整VC/俄文详情与主图建卡）")
    _add_shelve_args(p)

    return ap


def dispatch(args):
    from wb_ops.framework.registry import registry
    return registry.dispatch(args.cmd, args)



def main(argv=None):
    common.ensure_utf8_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        code = dispatch(args)
    except KeyboardInterrupt:
        print("\n已中断")
        code = 130
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[错误] {e}", file=sys.stderr)
        code = 1
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()

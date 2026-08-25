# -*- coding: utf-8 -*-
"""
wb_ops 统一配置（只放路径与业务常量，不含任何凭证）

凭证已迁移到 data/credentials.json，由 credentials.py 统一加载。
所有文件路径都从这里派生，改目录只需改本文件。
"""
import os

# ---------------- 目录 ----------------
# 仓库根 = wb_ops/ 的上一级
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(REPO_ROOT, "data")            # 统一数据目录
PRODUCTS_DIR = os.path.join(DATA_DIR, "products")     # 各店商品快照 shop{id}_products_all.json
STATE_DIR = os.path.join(DATA_DIR, "state")           # 同步状态 / 自动新增清单
HAR_DIR = os.path.join(DATA_DIR, "har")               # 抓包 md/har 凭证源（cookies-update 输入）
WORKBENCH_DIR = os.path.join(DATA_DIR, "workbench")   # 生成的工作台 *.html
LOG_DIR = os.path.join(DATA_DIR, "logs")              # 运行日志 + 结果 CSV

# ---------------- 文件路径 ----------------
CREDENTIALS_JSON = os.path.join(DATA_DIR, "credentials.json")   # 统一凭证（BCS + WB 5 店）
AI_TEST_QA = os.path.join(DATA_DIR, "ai_test_qa.json")   # AI 客服测试数据集（真人问答对照）
BOSS_XLSX = os.path.join(DATA_DIR, "商品价格表.xlsx")            # 唯一权威商品清单（用户维护）
MAPPING_XLSX = os.path.join(DATA_DIR, "价格映射表.xlsx")  # 唯一状态源（merge 自动重建）

STATUS_JSON = os.path.join(STATE_DIR, "多店铺同步状态.json")
AUTO_ADD_JSON = os.path.join(STATE_DIR, "多店铺自动新增清单.json")

# 工作台 HTML 输出
OUT_MAPPING_HTML = os.path.join(WORKBENCH_DIR, "价格映射核对工作台.html")
OUT_REVIEW_HTML = os.path.join(WORKBENCH_DIR, "多店铺待审核.html")
OUT_MAPPING_CHECK_HTML = os.path.join(WORKBENCH_DIR, "映射表核查工作台.html")
OUT_MISMATCH_HTML = os.path.join(WORKBENCH_DIR, "货不对板筛查工作台.html")

# ops 执行明细 CSV（追加）
RESULT_CSV = os.path.join(LOG_DIR, "ops_result.csv")


def shop_json_path(shop_id):
    """某店商品快照路径"""
    return os.path.join(PRODUCTS_DIR, f"shop{shop_id}_products_all.json")


# ---------------- 业务常量 ----------------
# 主店（映射表主数据源）：None = 取店铺列表第一个（bcs.fetch_shop_list 顺序）；也可显式指定某个店铺 ID
MAIN_SHOP = None

# vendorCode 标准格式：BCS-{4位前缀}-{WB原始nmId}（中段=商品前缀，商品价格表「vendorCode前缀码」列登记）。
# 兼容他人表 `ozon-card-` 尾段：BCS-{前缀}-ozon-card-{WB原始nmId}（WB商品码不变，前缀提取取 group(1)，两格式通吃）。
VC_PREFIX_RE = r"^BCS-([A-Z]{4})-(?:ozon-card-)?\d+$"

# 改折扣默认阈值/目标（>50% → 50%）
DISCOUNT_THRESHOLD_DEF = 50
DISCOUNT_TARGET_DEF = 50

# 默认操作仓库（改库存/下架清库存/回收站归零/上架等所有仓库操作默认只操作它）。
# 按仓库 name 匹配（BCS 仓库列表 /system/wbWarehouses/list 的 name 字段）。
# 成都仓库（国内仓，name="成都仓库"）默认不操作，需单独用 `wb.py remote-wh` 命令处理。
DEFAULT_WAREHOUSE_NAME = "莫斯科"


"""Promotion audit constants and report schema."""
MODE_GAP, MODE_WASTE, MODE_BOTH = "gap", "waste", "both"
SHOP_SLEEP = 0.5

DEFAULT_MIN_ORDERS = 4

DEFAULT_TABLE = "订单登记"

ACTIVE_STATUS_ID = 9

PAUSED_STATUS_ID = 11

KNOWN_STATUS_IDS = (ACTIVE_STATUS_ID, PAUSED_STATUS_ID)

ALL_MODES = (MODE_GAP, MODE_WASTE, MODE_BOTH)

GROUP_LISTED = "可直接补推"

GROUP_UNLISTED = "需先上架"

VERDICT_CLOSE = "建议关闭"

VERDICT_PAUSED = "已暂停无需操作"

VERDICT_UNKNOWN = "无法判定"

GAP_CSV_FIELDS = ["方向", "店铺", "店铺ID", "分组", "BCS编号", "商品中文名", "三店合计单数",
                  "该店单数", "各店单数拆分", "本店nmId", "备注"]

WASTE_CSV_FIELDS = ["店铺", "店铺ID", "活动ID", "活动名", "活动状态ID", "结算方式", "预算", "商品nmId",
                    "供应商代码", "商品中文名", "三店合计单数", "该店单数", "各店单数拆分",
                    "该nm本店被推广活动数", "判定", "备注"]

WASTE_CAMP_FIELDS = ["店铺", "店铺ID", "活动ID", "活动名", "活动状态ID", "结算方式", "预算",
                     "被推广商品数", "建议关闭数", "是否整活动建议关闭"]

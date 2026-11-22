"""
sellerops 核心领域模型与数据契约 (Domain Models & DTOs)

定义标准不可变数据实体，屏蔽异构平台字段命名分歧。
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple


@dataclass(slots=True, frozen=True)
class TaskResult:
    """异步任务或批次执行结果"""
    task_id: Optional[int] = None
    success: bool = True
    processed_count: int = 0
    error_message: str = ""
    shop_id: Optional[int] = None


@dataclass(slots=True, frozen=True)
class DiscountUploadResult(TaskResult):
    """WB 改折扣提交结果（两阶段提交的预检弹窗标记 + 任务号）。

    WB `upload/task` 为两阶段：`checkChange=true` 只做预检，返回是否需要弹窗
    （priceModal=降价提示 / quarantineModal=隔离区提示，实测 2026-09-17 抓包
    `api/网络请求/wb批量修改折扣+降价提示.har`），真正落库必须再以
    `checkChange=false` 提交并拿到 `data.id`。
    """
    outcome: str = "failed"
    auth_stopped: bool = False
    already_exists: bool = False
    price_modal: bool = False
    quarantine_modal: bool = False


@dataclass(slots=True, frozen=True)
class ComplaintProduct:
    """投诉单关联商品（来源：投诉详情 brands[].products[]）"""
    nm_id: int
    name: str = ""
    brand: str = ""
    image_url: str = ""


@dataclass(slots=True, frozen=True)
class Complaint:
    """WB 平台投诉单（callcenter supplier/appeals）

    decide_counter 为界面「剩余天数」，仅在 is_decide_allowed 为真时由平台下发，
    缺失时保持 None（不可与 0 混淆）。
    """
    appeal_id: int
    shop_id: int
    shop_name: str
    cro_company: str = ""
    theme_name: str = ""
    parent_theme_name: str = ""
    create_date: str = ""
    status_id: int = 0
    status: str = ""
    is_read: bool = False
    is_closed: bool = False
    is_decide_allowed: bool = False
    decide_counter: Optional[int] = None
    products: Tuple[ComplaintProduct, ...] = field(default_factory=tuple)
    raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)

    @classmethod
    def from_list_dict(
        cls,
        shop_id: int,
        shop_name: str,
        data: Dict[str, Any],
        products: Tuple[ComplaintProduct, ...] = (),
    ) -> "Complaint":
        """从列表接口（v1/supplier/appeals）的单条数据构建实体"""
        counter: Optional[int] = None
        if data.get("is_decide_allowed") and data.get("decide_counter") is not None:
            try:
                counter = int(data["decide_counter"])
            except (TypeError, ValueError):
                counter = None
        return cls(
            appeal_id=int(data.get("id") or 0),
            shop_id=int(shop_id or 0),
            shop_name=str(shop_name or ""),
            cro_company=str(data.get("cro_company") or ""),
            theme_name=str(data.get("theme_name") or ""),
            parent_theme_name=str(data.get("parent_theme_name") or ""),
            create_date=str(data.get("create_date") or ""),
            status_id=int(data.get("status_id") or 0),
            status=str(data.get("status") or ""),
            is_read=bool(data.get("is_read", False)),
            is_closed=bool(data.get("is_closed", False)),
            is_decide_allowed=bool(data.get("is_decide_allowed", False)),
            decide_counter=counter,
            products=tuple(products),
            raw_data=data,
        )


@dataclass
class PublishItem:
    """上架商品数据载荷领域实体"""
    nm_id: int                                   # WB 商品码 (sku)
    price: Optional[float] = None                # 售价 (CNY)
    vendor_code: Optional[str] = None            # 完整 vendorCode (如 BCS-ABCD-123456 或自定义)
    prefix: Optional[str] = None                 # 4位前缀码 (如 ABCD 或 BCS-ABCD)
    length: Optional[int] = None                 # 包装长 (cm)
    width: Optional[int] = None                  # 包装宽 (cm)
    height: Optional[int] = None                 # 包装高 (cm)
    weight: Optional[float] = None               # 包装毛重 (kg)
    stock: int = 999                             # 仓库库存
    target_shops: Optional[List[int]] = None     # 目标店铺列表 (None=全部)
    cn: str = ""                                 # 中文名 (可选，用于辅助匹配/日志)
    title: str = ""                              # 俄文标题 (可选，旧接口需要)
    main_image: str = ""                         # 主图链接 (可选，旧接口需要)
    images: str = ""                             # 附图分号拼接 (可选，旧接口需要)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ProductRatingItem:
    """商品链接评分与买家评价领域实体"""
    shop_id: int
    shop_name: str
    nm_id: int
    vendor_code: str
    name: str = ""                               # 平台商品标题（俄语）
    rating: float = 0.0                          # 卡片质量评分 (1-10)
    feedback_rating: float = 0.0                 # 买家评价评分 (0-5.0)
    feedback_count: int = 0                      # 评价总数
    one_star: int = 0                            # 1星评价数
    two_star: int = 0                            # 2星评价数
    three_star: int = 0                          # 3星评价数
    four_star: int = 0                           # 4星评价数
    five_star: int = 0                           # 5星评价数
    is_shadowed: bool = False                    # 是否被平台屏蔽
    cn_name: str = ""                            # 产品中文名（商品价格表）
    price: Optional[float] = None                # 售价（商品价格表实际售价）
    dimensions: str = ""                         # 尺寸（商品价格表）
    prefix: str = ""                             # vendorCode前缀码（商品价格表）
    source_wb: str = "商品评分API"               # wb编号来源

# -*- coding: utf-8 -*-
"""
wb_ops 核心领域模型与数据契约 (Domain Models & DTOs)

定义标准不可变数据实体，屏蔽异构平台字段命名分歧。
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple


@dataclass(slots=True, frozen=True)
class Product:
    """标准商品领域实体"""
    nm_id: int
    vendor_code: str
    cn_name: str = ""
    current_discount: int = 0
    current_price: float = 0.0
    currency: str = "CNY"
    title: str = ""
    shop_id: Optional[int] = None
    raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)

    @property
    def is_valid(self) -> bool:
        return bool(self.nm_id and self.vendor_code)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为基础字典，保持向下兼容"""
        return {
            "nmID": self.nm_id,
            "nmId": self.nm_id,
            "vendorCode": self.vendor_code,
            "goodsCode": self.vendor_code,
            "cn": self.cn_name,
            "discount": self.current_discount,
            "price": self.current_price,
            "isoCode4217": self.currency,
            "title": self.title,
            "shopId": self.shop_id,
        }

    @classmethod
    def from_wb_dict(cls, data: Dict[str, Any], cn_name: str = "", shop_id: Optional[int] = None) -> "Product":
        """从 WB 原生返回的 listGoods 字典反序列化"""
        prices = data.get("prices") or []
        price_val = float(prices[0]) if prices and prices[0] is not None else 0.0
        return cls(
            nm_id=int(data.get("nmID") or data.get("nmId") or 0),
            vendor_code=str(data.get("vendorCode") or "").strip(),
            cn_name=cn_name or str(data.get("cn") or ""),
            current_discount=int(data.get("discount") or 0),
            current_price=price_val,
            currency=str(data.get("isoCode4217") or "CNY"),
            title=str(data.get("title") or ""),
            shop_id=shop_id,
            raw_data=data,
        )

    @classmethod
    def from_snapshot_dict(cls, data: Dict[str, Any], shop_id: Optional[int] = None) -> "Product":
        """从本地快照字典反序列化"""
        return cls(
            nm_id=int(data.get("nmId") or data.get("nmID") or 0),
            vendor_code=str(data.get("vendorCode") or data.get("goodsCode") or "").strip(),
            cn_name=str(data.get("cn") or ""),
            current_discount=int(data.get("discount") or 0),
            current_price=float(data.get("price") or data.get("salePrice") or 0.0),
            currency=str(data.get("currency") or "CNY"),
            title=str(data.get("title") or data.get("shopGoodsName") or ""),
            shop_id=shop_id or data.get("shopId"),
            raw_data=data,
        )


@dataclass(slots=True, frozen=True)
class Shop:
    """标准店铺实体"""
    shop_id: int
    shop_name: str
    authorizev3: str = ""
    wb_seller_lk: str = ""
    cookie: str = ""
    is_active: bool = True

    @property
    def has_full_credentials(self) -> bool:
        return bool(self.authorizev3 and self.wb_seller_lk and self.cookie)


@dataclass(slots=True, frozen=True)
class DiscountPlan:
    """折扣批量修改规划输入模型

    threshold/below 为「折扣区间」双侧条件，取并集：
      - threshold >= 0 → 命中「折扣 > threshold」
      - below >= 0     → 命中「折扣 < below」
      - 两者都为 -1 或 is_all=True → 不限折扣
    例：threshold=55, below=40 → 「>55% 或 <40%」。
    """
    threshold: int = 50
    below: int = -1
    target_discount: int = 50
    name_filter: str = ""
    prefix_filter: str = ""
    target_vcs: Optional[List[str]] = None
    target_shops: Optional[List[int]] = None
    limit: int = 0
    chunk_size: int = 100
    is_apply: bool = False
    verify_after: bool = False
    is_all: bool = False


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
    already_exists: bool = False
    price_modal: bool = False
    quarantine_modal: bool = False


# 别名兼容
ProductCard = Product


class OrderStatus:
    """订单标准状态枚举"""
    NEW = "NEW"
    WAITING_SHIPPED = "WAITING_SHIPPED"
    SHIPPED = "SHIPPED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True, frozen=True)
class CustomStockPlan:
    """自定义库存调整规划模型"""
    target_stock: int = 0
    name_filter: str = ""
    prefix_filter: str = ""
    target_vcs: Optional[List[str]] = None
    target_shops: Optional[List[int]] = None
    is_apply: bool = False
    skip_confirmation: bool = False
    sync_after: bool = False


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

    @property
    def is_pending(self) -> bool:
        """是否为「未处理」投诉（等待回复）"""
        return self.status_id == 1

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


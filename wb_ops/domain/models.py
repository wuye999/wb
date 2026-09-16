# -*- coding: utf-8 -*-
"""
wb_ops 核心领域模型与数据契约 (Domain Models & DTOs)

定义标准不可变数据实体，屏蔽异构平台字段命名分歧。
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


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
    """折扣批量修改规划输入模型"""
    threshold: int = 50
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

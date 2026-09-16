# -*- coding: utf-8 -*-
"""
wb_ops 核心业务用例服务层 (Application Services)
按业务领域高内聚划分 5 大核心服务：
- DiscountService: 折扣改价与促销活动管理
- CatalogService: 映射总表、多店独立映射表与核对工作台
- OrderService: 订单履约、马帮 ERP 与飞书多维表格集成
- ReplicationService: 跨店复制上架与草稿/回收站清理
- CustomerSupportService: 买家提问监听与 AI 大模型智能应答
"""
from .discount_svc import DiscountService, discount_svc
from .catalog_svc import CatalogService, catalog_svc
from .order_svc import OrderService, order_svc
from .replicate_svc import ReplicationService, replicate_svc
from .support_svc import CustomerSupportService, support_svc

__all__ = [
    "DiscountService",
    "discount_svc",
    "CatalogService",
    "catalog_svc",
    "OrderService",
    "order_svc",
    "ReplicationService",
    "replicate_svc",
    "CustomerSupportService",
    "support_svc",
]

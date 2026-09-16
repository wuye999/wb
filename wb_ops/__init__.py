# -*- coding: utf-8 -*-
"""
wb_ops —— Wildberries/BCS 卖家自动化库（轻量分层整洁架构）

分层结构：
  表现与调度层: cli.py, daily.py, schedule.py
  业务用例服务层: services/ (catalog_svc, discount_svc, order_svc, replicate_svc, support_svc)
  领域模型层: domain/ (Product, Shop, DiscountPlan, TaskResult)
  仓储持久化层: storage/ (product_repo, mapping_repo)
  外部适配层: adapters/ (wb_client, bcs_client, llm_client, task_runner, cookies)
  核心框架基础设施: framework/ (safe_io, exceptions, registry)
"""

__version__ = "2.1.0"
__all__ = [
    "config",
    "credentials",
    "common",
    "daily",
    "schedule",
    "cli",
    "framework",
    "domain",
    "storage",
    "adapters",
    "services",
]

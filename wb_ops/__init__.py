# -*- coding: utf-8 -*-
"""
wb_ops —— Wildberries/BCS 卖家自动化库（统一入口）

把原「检查价格」+「促销折扣」两套脚本整合为一个职责清晰的 Python 包。

模块分层：
  入口层   cli.py            统一 CLI（子命令聚合）
  业务层   mapping / mapping_sync / mapping_check / ops
           promo / discount / clean / cookies / daily / schedule
           price_review / orders / questions
  支撑层   bcs / wb_api / products / workbench / keywords
           common / credentials / config

对外统一入口：仓库根目录的 wb.py（或 python -m wb_ops <子命令>）。
详细说明见 docs/ 下的 ARCHITECTURE.md / CLI.md / USAGE.md / CREDENTIALS.md。
"""

__version__ = "2.0.0"
__all__ = ["config", "credentials", "common", "bcs", "wb_api", "keywords",
           "products", "mapping", "mapping_sync", "mapping_check", "mismatch_check", "workbench",
           "ops", "promo", "discount", "clean", "cookies", "daily", "schedule",
           "price_review", "orders", "questions", "cli"]

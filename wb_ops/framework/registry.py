# -*- coding: utf-8 -*-
"""
wb_ops CLI 动态命令注册中心 (CommandRegistry)
支持按需动态加载模块，解除 CLI 与业务模块的静态编译期耦合。
"""
import importlib
from typing import Dict, Any, Callable, Optional


class CommandRegistry:
    """命令注册与分发器"""

    def __init__(self):
        self._handlers: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, module_path: str, func_name: str = "run", alias: Optional[str] = None):
        """注册命令路由"""
        meta = {
            "module": module_path,
            "func": func_name,
        }
        self._handlers[name] = meta
        if alias:
            self._handlers[alias] = meta

    def dispatch(self, cmd_name: str, args: Any) -> int:
        """根据命令名动态加载模块并执行"""
        entry = self._handlers.get(cmd_name)
        if not entry:
            raise ValueError(f"未注册的命令: {cmd_name}")

        mod = importlib.import_module(entry["module"])
        handler = getattr(mod, entry["func"])
        return handler(args)


registry = CommandRegistry()

# 注册全部 39 个命令的动态模块映射
registry.register("shops", "wb_ops.adapters.bcs_client", "print_shops")
registry.register("fetch", "wb_ops.services.catalog_svc", "run_fetch")
registry.register("mapping", "wb_ops.services.catalog_svc", "run_mapping")
registry.register("mapping-import", "wb_ops.services.catalog_svc", "run_mapping_import")
registry.register("mapping-check", "wb_ops.services.catalog_svc", "run_mapping_check")
registry.register("mismatch-check", "wb_ops.services.catalog_svc", "run_mismatch_check")
registry.register("review", "wb_ops.services.catalog_svc", "run_review")
registry.register("merge", "wb_ops.services.catalog_svc", "run_merge", alias="mapping-merge")
registry.register("mapping-rename", "wb_ops.services.catalog_svc", "run_mapping_rename")
registry.register("shops-mapping", "wb_ops.services.catalog_svc", "run_shops_mapping", alias="mapping-sync")
registry.register("price", "wb_ops.services.replicate_svc", "run_price")
registry.register("stock", "wb_ops.services.replicate_svc", "run_stock")
registry.register("trash", "wb_ops.services.replicate_svc", "run_trash")
registry.register("replicate", "wb_ops.services.replicate_svc", "run_replicate")
registry.register("import-shelve", "wb_ops.services.replicate_svc", "run_import_shelve")
registry.register("promo-apply", "wb_ops.services.discount_svc", "run_promo_apply")
registry.register("discount", "wb_ops.services.discount_svc", "run_cli")
registry.register("discount-wb", "wb_ops.services.discount_svc", "run_cli")
registry.register("discount-scan", "wb_ops.services.discount_svc", "run_cli")
registry.register("discount-bcs", "wb_ops.services.discount_svc", "run_discount_bcs")
registry.register("dimension", "wb_ops.services.replicate_svc", "run_dimension")
registry.register("dims-check", "wb_ops.services.replicate_svc", "run_dims_check")
registry.register("banned", "wb_ops.services.replicate_svc", "run_banned")
registry.register("clean", "wb_ops.services.replicate_svc", "run_clean")
registry.register("price-review", "wb_ops.services.discount_svc", "run_price_review")
registry.register("orders", "wb_ops.services.order_svc", "run_orders")
registry.register("questions", "wb_ops.services.support_svc", "run_questions")
registry.register("questions-watch", "wb_ops.services.support_svc", "run_questions_watch")
registry.register("ai-test", "wb_ops.services.support_svc", "run_ai_test")
registry.register("mabang-orders", "wb_ops.services.order_svc", "run_mabang_orders")
registry.register("mabang-forecast", "wb_ops.services.order_svc", "run_mabang_forecast")
registry.register("feishu-register", "wb_ops.services.order_svc", "run_feishu_register")
registry.register("mabang-stock-register", "wb_ops.services.order_svc", "run_mabang_stock_register")
registry.register("mabang-stock-daily", "wb_ops.services.order_svc", "run_mabang_stock_daily")
registry.register("mabang-process", "wb_ops.services.order_svc", "run_mabang_process", alias="order-pipeline")
registry.register("cookies-update", "wb_ops.adapters.cookies", "run_cookies_update")
registry.register("daily", "wb_ops.daily", "run_daily_cmd")
registry.register("schedule", "wb_ops.schedule", "run")
registry.register("remote-wh", "wb_ops.services.replicate_svc", "run_remote_wh")


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

# 注册全部 34 个命令的动态模块映射
registry.register("shops", "wb_ops.bcs", "print_shops")
registry.register("fetch", "wb_ops.products", "run")
registry.register("mapping", "wb_ops.workbench", "run")
registry.register("mapping-import", "wb_ops.mapping", "run_import")
registry.register("mapping-merge", "wb_ops.mapping", "run_merge")
registry.register("mapping-split", "wb_ops.mapping", "run_split")
registry.register("mapping-clean", "wb_ops.mapping", "run_clean")
registry.register("mapping-sync", "wb_ops.mapping_sync", "run")
registry.register("mapping-check", "wb_ops.mapping_check", "run")
registry.register("mismatch-check", "wb_ops.mismatch_check", "run")
registry.register("dims-check", "wb_ops.dims_check", "run")
registry.register("dimension", "wb_ops.dimension", "run")
registry.register("banned", "wb_ops.banned", "run")
registry.register("price-calc", "wb_ops.ops", "run_price_calc")
registry.register("price-update", "wb_ops.ops", "run_price_update")
registry.register("price-sync", "wb_ops.ops", "run_price_sync")
registry.register("price-review", "wb_ops.price_review", "run")
registry.register("promo-list", "wb_ops.promo", "run_list")
registry.register("promo-apply", "wb_ops.promo", "run_apply")
registry.register("discount", "wb_ops.discount", "run")
registry.register("discount-wb", "wb_ops.discount_wb", "run")
registry.register("discount-scan", "wb_ops.discount_scan", "run")
registry.register("discount-bcs", "wb_ops.discount_bcs", "run")
registry.register("clean", "wb_ops.clean", "run")
registry.register("replicate", "wb_ops.replicate", "run")
registry.register("import-shelve", "wb_ops.import_shelve", "run")
registry.register("orders", "wb_ops.orders", "run")
registry.register("order-pipeline", "wb_ops.order_pipeline", "run")
registry.register("questions", "wb_ops.questions", "run")
registry.register("questions-watch", "wb_ops.questions_watch", "run")
registry.register("ai-test", "wb_ops.ai_reply_test", "run")
registry.register("mabang-orders", "wb_ops.mabang", "run")
registry.register("mabang-forecast", "wb_ops.mabang", "run_forecast")
registry.register("feishu-register", "wb_ops.feishu_register", "run")
registry.register("mabang-process", "wb_ops.mabang_process", "run")
registry.register("mabang-stock-register", "wb_ops.mabang_stock", "run")
registry.register("mabang-stock-daily", "wb_ops.mabang_stock", "run_daily")
registry.register("cookies-update", "wb_ops.cookies", "run")
registry.register("daily", "wb_ops.daily", "run")
registry.register("schedule", "wb_ops.schedule", "run")
registry.register("remote-wh", "wb_ops.remote_wh", "run")

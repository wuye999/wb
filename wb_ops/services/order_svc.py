# -*- coding: utf-8 -*-
"""
wb_ops 订单履约、马帮 ERP 与多维表格协同业务服务 (OrderService)
统一管理马帮待处理订单匹配更换、预报批次、自动化流水线交运与飞书多维表格同步。
"""
from typing import Any
from .order import (
    mabang,
    mabang_process,
    mabang_stock,
    feishu_register,
    orders,
)


class OrderService:
    """订单履约与 ERP 服务"""

    def match_and_replace_skus(self, args: Any) -> int:
        """马帮待处理订单 SKU 匹配核对/更换"""
        return mabang.run(args)

    def forecast_batches(self, args: Any) -> int:
        """马帮预报批次生成与上传"""
        return mabang.run_forecast(args)

    def register_to_feishu(self, args: Any) -> int:
        """马帮订单登记到飞书多维表格"""
        return feishu_register.run(args)

    def process_pipeline(self, args: Any) -> int:
        """马帮订单一体化管道：匹配→预报→发货→交运"""
        return mabang_process.run(args)

    def register_stock(self, args: Any) -> int:
        """马帮全部库存 SKU 重建飞书库存登记表"""
        return mabang_stock.run(args)

    def manage_stock_daily(self, args: Any) -> int:
        """马帮库存登记表日期列管理"""
        return mabang_stock.run_daily(args)

    def query_bcs_orders(self, args: Any) -> int:
        """BCS 订单查询与同步"""
        return orders.run(args)


order_svc = OrderService()

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
    feishu_register_dedup,
    feishu_vc_stats,
    fbs_delivery,
    order_stats,
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

    def dedup_feishu_register(self, args: Any) -> int:
        """飞书「订单登记」存量重复清理（按订单编号分组，保留表内最早一条）"""
        return feishu_register_dedup.run(args)

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

    def count_register_by_vendor(self, args: Any) -> int:
        """飞书「订单登记」按供应商代码统计单数（只读）"""
        return feishu_vc_stats.run(args)

    def collect_delivery_goods(self, args: Any) -> int:
        """WB FBS「配送中」清单及其商品采集与统计（只读）"""
        return fbs_delivery.run(args)

    def collect_delivery_goods_data(self, **kwargs) -> dict:
        """★ 门面库入口（跨域只经门面）：WB FBS「配送中」清单商品采集统计（只读）。"""
        return fbs_delivery.delivery_goods(**kwargs)

    def collect_order_stats(self, args: Any) -> int:
        """WB「订单动态」近一月订单按商品中文名统计（只读，输出 xlsx 多 Sheet）"""
        return order_stats.run(args)

    def order_stats_data(self, **kwargs) -> dict:
        """★ 门面库入口（跨域只经门面）：WB「订单动态」按商品中文名统计（只读）。"""
        return order_stats.order_stats(**kwargs)

    def feishu_register_stats(self, days: int = 7, begin: str = "", end: str = "",
                              date: str = "", shops: Any = None, url: str = "",
                              table: str = "订单登记", with_cn: bool = True) -> dict:
        """★ 门面库入口（跨域只经门面，REUSE_GUIDE 铁律 3）：飞书「订单登记」统计（只读）。

        返回 == `feishu_vc_stats.vc_order_stats(...)`（含 'begin'/'end'/'raw_rows'/'stats'），
        供其它域（如 discount 域 promo-gap）复用，避免跨域 import order 域私有实现。
        """
        return feishu_vc_stats.vc_order_stats(days=days, begin=begin, end=end, date=date,
                                             shops=shops, url=url, table=table,
                                             by_prefix=False, with_cn=with_cn)


order_svc = OrderService()


def run_orders(args):
    return order_svc.query_bcs_orders(args)


def run_mabang_orders(args):
    return order_svc.match_and_replace_skus(args)


def run_mabang_forecast(args):
    return order_svc.forecast_batches(args)


def run_feishu_register(args):
    return order_svc.register_to_feishu(args)


def run_feishu_register_dedup(args):
    return order_svc.dedup_feishu_register(args)


def run_mabang_process(args):
    return order_svc.process_pipeline(args)


def run_mabang_stock_register(args):
    return order_svc.register_stock(args)


def run_mabang_stock_daily(args):
    return order_svc.manage_stock_daily(args)


def run_feishu_vc_stats(args):
    return order_svc.count_register_by_vendor(args)


def run_fbs_delivery(args):
    return order_svc.collect_delivery_goods(args)


def run_order_stats(args):
    return order_svc.collect_order_stats(args)


def feishu_register_stats(**kwargs):
    """模块级薄转发（供其它域 import：`from wb_ops.services.order_svc import feishu_register_stats`）"""
    return order_svc.feishu_register_stats(**kwargs)


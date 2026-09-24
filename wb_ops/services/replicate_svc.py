# -*- coding: utf-8 -*-
"""
wb_ops 商品搬家、跨店复制、库存与清理用例服务 (ReplicationService)
统一管理跨店铺商品复制上架、他人映射表差集导入、草稿/回收站清理、违规商品处理与库存/下架操作。
"""
from typing import Any
from .replicate import (
    replicate,
    import_shelve,
    shelve_new,
    shelve_old,
    clean,
    remote_wh,
    dimension,
    dims_check,
    banned,
    ops,
    stock_wb,
    price_wb,
)


class ReplicationService:
    """商品搬家、库存与运维服务"""

    def shelve_batch_new(self, args: Any) -> int:
        """新版批量上品用例"""
        return shelve_new.run(args)

    def shelve_legacy_old(self, args: Any) -> int:
        """旧版上品建卡用例"""
        return shelve_old.run(args)

    def replicate_across_shops(self, args: Any) -> int:
        """跨店复制上架用例"""
        return replicate.run(args)

    def import_external_shelve(self, args: Any) -> int:
        """他人映射表导入上架用例"""
        return import_shelve.run(args)

    def clean_drafts_and_trash(self, args: Any) -> int:
        """清理草稿箱与回收站用例"""
        return clean.run(args)

    def delete_remote_warehouse_stocks(self, args: Any) -> int:
        """远程仓库货品删除用例"""
        return remote_wh.run(args)

    def manage_dimensions(self, args: Any) -> int:
        """商品尺寸规格修改"""
        return dimension.run(args)

    def check_dimensions(self, args: Any) -> int:
        """商品尺寸规格合规检查"""
        return dims_check.run(args)

    def handle_banned_products(self, args: Any) -> int:
        """违规受阻商品扫描与移入回收站"""
        return banned.run(args)

    def handle_ops(self, action: str, args: Any) -> int:
        """一键库存/下架操作 (stock / trash)"""
        return ops.run(action, args)

    def run_stock_wb_native(self, args: Any) -> int:
        """改库存默认通道（2026-09-24 起）：WB 原生在线接口（portal stocks）；
        `stock` 与 `stock-wb` 均路由至此。"""
        return stock_wb.run(args)

    def run_stock_bcs(self, args: Any) -> int:
        """改库存备选通道：BCS stock/batchSetByChrtIdsBatch（`stock-bcs` 命令）。"""
        return ops.run("stock", args)

    def run_price_wb_native(self, args: Any) -> int:
        """改价默认通道（2026-09-24 起）：WB 原生 dp-api 批量（upload/task，预检后自动确认）；
        `price` 与 `price-wb` 均路由至此。"""
        return price_wb.run(args)

    def run_price_bcs(self, args: Any) -> int:
        """改价备选通道：BCS price/batch（`price-bcs` 命令）。"""
        return ops.run("price", args)

    def set_stock_wb(self, shop: dict, warehouse_id: int, chrt_items: list, **kw) -> dict:
        """跨域脚本入口（铁律 3）：单店批量设库存（WB 原生 portal 接口）"""
        return stock_wb.set_stock(shop, warehouse_id, chrt_items, **kw)

    def apply_prices_wb(self, shop: dict, items: list, **kw) -> dict:
        """跨域脚本入口（铁律 3）：单店批量改价（WB 原生 dp-api upload/task）"""
        return price_wb.apply_prices(shop, items, **kw)


replicate_svc = ReplicationService()


def run_price(args):
    """[备选] BCS 通道改价（price-bcs 命令；price/price-wb 默认走 WB 原生 dp-api）"""
    return replicate_svc.run_price_bcs(args)


def run_price_wb(args):
    """[默认] WB 原生 dp-api 批量改价（price 与 price-wb 命令）"""
    return replicate_svc.run_price_wb_native(args)


def run_price_bcs(args):
    return replicate_svc.run_price_bcs(args)


def run_stock(args):
    """[备选] BCS 通道改库存（stock-bcs 命令；stock/stock-wb 默认走 WB 原生）"""
    return replicate_svc.run_stock_bcs(args)


def run_stock_wb(args):
    """[默认] WB 原生在线接口改库存（stock 与 stock-wb 命令）"""
    return replicate_svc.run_stock_wb_native(args)


def run_stock_bcs(args):
    return replicate_svc.run_stock_bcs(args)


def run_trash(args):
    return replicate_svc.handle_ops("trash", args)


def run_replicate(args):
    return replicate_svc.replicate_across_shops(args)


def run_import_shelve(args):
    return replicate_svc.import_external_shelve(args)


def run_dimension(args):
    return replicate_svc.manage_dimensions(args)


def run_dims_check(args):
    return replicate_svc.check_dimensions(args)


def run_banned(args):
    return replicate_svc.handle_banned_products(args)


def run_clean(args):
    return replicate_svc.clean_drafts_and_trash(args)


def run_remote_wh(args):
    return replicate_svc.delete_remote_warehouse_stocks(args)


def run_shelve(args):
    return replicate_svc.shelve_batch_new(args)


def run_shelve_old(args):
    return replicate_svc.shelve_legacy_old(args)



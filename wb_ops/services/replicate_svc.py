# -*- coding: utf-8 -*-
"""
wb_ops 商品搬家、跨店复制、库存与清理用例服务 (ReplicationService)
统一管理跨店铺商品复制上架、他人映射表差集导入、草稿/回收站清理、违规商品处理与库存/下架操作。
"""
from typing import Any
from .replicate import (
    replicate,
    import_shelve,
    clean,
    remote_wh,
    dimension,
    dims_check,
    banned,
    ops,
)


class ReplicationService:
    """商品搬家、库存与运维服务"""

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


replicate_svc = ReplicationService()


def run_price(args):
    return replicate_svc.handle_ops("price", args)


def run_stock(args):
    return replicate_svc.handle_ops("stock", args)


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


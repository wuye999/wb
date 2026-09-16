# -*- coding: utf-8 -*-
"""
wb_ops 商品目录、映射与核对工作台业务服务 (CatalogService)
统一管理价格映射表构建、多店铺独立单表生成、核查、快照拉取与货不对板筛查用例。
"""
from typing import Optional, List, Dict, Any
from ..storage.mapping_repo import MappingRepository
from ..storage.product_repo import ProductSnapshotRepository
from .catalog import mapping, mapping_sync, mapping_check, mismatch_check, products


class CatalogService:
    """商品映射与核对工作台服务"""

    def __init__(self):
        self.mapping_repo = MappingRepository
        self.product_repo = ProductSnapshotRepository

    def fetch_shop_products(self, shop_id: int, out_file: str, no_sync: bool = False):
        """拉取指定店铺商品数据快照"""
        return products.fetch_shop(shop_id, out_file, no_sync=no_sync)

    def fetch_all_shops_products(self, no_sync: bool = False):
        """拉取全部店铺商品数据快照"""
        return products.fetch_all(no_sync=no_sync)

    def generate_mapping_workbench(self, legacy: bool = False):
        """生成统一核对工作台（5 店并集，一页两区）"""
        return mapping.run_mapping(legacy=legacy)

    def import_mapping(self, file_path: str):
        """导入核对结果并生成映射表"""
        return mapping.import_mapping(file_path)

    def check_mapping_integrity(self, tol: float = 0.05):
        """映射表核查（带图，核对匹配可疑项）"""
        return mapping_check.run(tol=tol)

    def check_mismatches(self, cn: str = "", begin: str = "", end: str = "", days: int = 0):
        """货不对板筛查工作台"""
        return mismatch_check.run(cn=cn, begin=begin, end=end, days=days)

    def run_review(self):
        """多店铺待审核工作台"""
        return mapping_sync.run_review()

    def run_merge(self, file_path: Optional[str] = None):
        """增量合并审核 → 映射表"""
        return mapping_sync.run_merge(file_path)

    def sync_all_shops_mapping(self, shop_id: Optional[int] = None, force: bool = False):
        """刷新/生成各店铺独立映射表（data/shops/shop_*.xlsx）"""
        return mapping_sync.sync_all_shops_mapping(shop_id=shop_id, force=force)

    def set_vc_override(self, vc: str, new_cn: str, reason: str = "人工纠偏", file_path: str = "") -> bool:
        """人工纠偏/修改商品中文名"""
        return mapping_sync.set_vc_override(vc=vc, new_cn=new_cn, reason=reason, file_path=file_path)


catalog_svc = CatalogService()

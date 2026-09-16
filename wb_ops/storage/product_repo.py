# -*- coding: utf-8 -*-
"""
wb_ops 商品快照仓储 (ProductSnapshotRepository)
统一管理各店铺本地商品快照文件 (shop{id}_products_all.json) 的安全原子读写与 VC 索引。
"""
import os
from typing import Dict, List, Optional, Any
from .. import config
from ..framework.safe_io import atomic_dump_json, safe_load_json


class ProductSnapshotRepository:
    """商品快照仓储类"""

    _cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def get_path(shop_id: Any) -> str:
        """获取某店铺商品快照绝对路径"""
        return config.shop_json_path(shop_id)

    @classmethod
    def exists(cls, shop_id: Any) -> bool:
        """快照是否存在"""
        return os.path.exists(cls.get_path(shop_id))

    @classmethod
    def load_shop_products(cls, shop_id: Any) -> List[Dict[str, Any]]:
        """安全加载某店铺全量商品快照列表。若不存在返回空列表。"""
        path = cls.get_path(shop_id)
        data = safe_load_json(path, default={})
        if isinstance(data, dict):
            return data.get("rows", [])
        elif isinstance(data, list):
            return data
        return []

    @classmethod
    def save_shop_products(cls, shop_id: Any, products: List[Dict[str, Any]], use_lock: bool = True):
        """原子安全保存店铺全量商品快照。"""
        path = cls.get_path(shop_id)
        # 兼容现有格式包装
        payload = {
            "shopId": int(shop_id),
            "total": len(products),
            "rows": products,
        }
        atomic_dump_json(path, payload, indent=2, use_lock=use_lock)
        sid_str = str(shop_id)
        if sid_str in cls._cache:
            del cls._cache[sid_str]

    @classmethod
    def load_shop_rows(cls, shop_id: Any) -> Optional[Dict[str, Dict[str, Any]]]:
        """按 vendorCode 构建在架商品索引字典（完全兼容原 ops.load_shop_rows 输出契约）。
        
        过滤已进回收站商品 (not r.get("trashedAt"))。
        若文件不存在返回 None。
        """
        sid_str = str(shop_id)
        if sid_str in cls._cache:
            return cls._cache[sid_str]

        path = cls.get_path(shop_id)
        if not os.path.exists(path):
            return None

        products = cls.load_shop_products(shop_id)
        rows: Dict[str, Dict[str, Any]] = {}
        for item in products:
            if not isinstance(item, dict):
                continue
            if item.get("trashedAt"):
                continue
            vc = item.get("vendorCode") or item.get("goodsCode") or ""
            if vc:
                rows[vc.strip()] = item

        cls._cache[sid_str] = rows
        return rows

    @classmethod
    def shop_ids_from_disk(cls) -> List[int]:
        """扫描磁盘上所有店铺商品快照文件获取店铺 ID 列表"""
        import glob
        ids = []
        for p in sorted(glob.glob(config.shop_json_path("*"))):
            try:
                sid = int(os.path.basename(p).replace("shop", "").replace("_products_all.json", ""))
                ids.append(sid)
            except ValueError:
                continue
        return ids


# 模块级便捷访问导出
ProductRepository = ProductSnapshotRepository
load_shop_products = ProductSnapshotRepository.load_shop_products
save_shop_products = ProductSnapshotRepository.save_shop_products
load_shop_rows = ProductSnapshotRepository.load_shop_rows
shop_ids_from_disk = ProductSnapshotRepository.shop_ids_from_disk


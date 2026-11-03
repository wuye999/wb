"""
sellerops 商品快照仓储 (ProductSnapshotRepository)
统一管理各店铺本地商品快照文件 (shop{id}_products_all.json) 的安全原子读写与 VC 索引。
"""
import os
from typing import Dict, List, Optional, Any
from sellerops.settings import paths as config
from sellerops.core.files import atomic_dump_json
from sellerops.core.files import safe_load_json


class ProductSnapshotRepository:
    """商品快照仓储类"""

    _cache: Dict[tuple[str, int, int], Dict[str, Any]] = {}

    @staticmethod
    def get_path(shop_id: Any) -> str:
        """获取某店铺商品快照绝对路径"""
        return config.shop_json_path(shop_id)

    @classmethod
    def exists(cls, shop_id: Any) -> bool:
        """快照是否存在"""
        return os.path.exists(cls.get_path(shop_id))

    @classmethod
    def load_snapshot_document(cls, shop_id: Any) -> Optional[Dict[str, Any]]:
        """读取行和采集元信息；不存在返回 None，损坏或错店不作为空店。"""
        path = cls.get_path(shop_id)
        if not os.path.isfile(path):
            return None
        data = safe_load_json(path)
        if isinstance(data, list):
            data = {'rows': data}
        if not isinstance(data, dict) or not isinstance(data.get('rows'), list):
            raise ValueError('商品快照缺少 rows 数组')
        if any(not isinstance(row, dict) for row in data['rows']):
            raise ValueError('商品快照包含无效商品行')
        if data.get('shopId') is not None and str(data['shopId']) != str(shop_id):
            raise ValueError('商品快照的 shopId 与目标店铺不一致')
        for key in ('total', 'fetchedCount'):
            if data.get(key) is not None and data[key] != len(data['rows']):
                raise ValueError('商品快照数量不完整或元信息不一致')
        return data

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
        cls._cache = {key: value for key, value in cls._cache.items() if key[0] != path}

    @classmethod
    def load_shop_rows(cls, shop_id: Any) -> Optional[Dict[str, Dict[str, Any]]]:
        """按 vendorCode 构建在架商品索引字典（完全兼容原 ops.load_shop_rows 输出契约）。

        过滤已进回收站商品 (not r.get("trashedAt"))。
        若文件不存在返回 None。
        """
        path = cls.get_path(shop_id)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return None
        cache_key = (path, stat.st_mtime_ns, stat.st_size)
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        document = cls.load_snapshot_document(shop_id)
        products = document['rows'] if document is not None else []
        rows: Dict[str, Dict[str, Any]] = {}
        for item in products:
            if not isinstance(item, dict):
                continue
            if item.get("trashedAt"):
                continue
            vc = item.get("vendorCode") or item.get("goodsCode") or ""
            if vc:
                if not isinstance(vc, str) or not vc.strip() or vc.strip() in rows:
                    raise ValueError('商品快照包含无效或重复vendorCode，无法唯一定位商品')
                rows[vc.strip()] = item

        cls._cache = {key: value for key, value in cls._cache.items() if key[0] != path}
        cls._cache[cache_key] = rows
        return rows

    @classmethod
    def shop_ids_from_disk(cls) -> List[int]:
        """扫描磁盘上所有店铺商品快照文件获取店铺 ID 列表"""
        from pathlib import Path
        ids = []
        for p in sorted(Path(config.PRODUCTS_DIR).glob('shop*_products_all.json')):
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

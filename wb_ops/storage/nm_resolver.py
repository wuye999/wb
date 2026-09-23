# -*- coding: utf-8 -*-
"""
wb_ops nmId → 供应商代码 / 中文名 本地反查（storage 层公共件）

从 services/support/complaints.py 的私有 _LocalResolver 原样上移，
供多个业务域（appeals 投诉单 / promo-goods 推广商品 / …）共用，
避免跨域 import 别域私有实现（docs/REUSE_GUIDE.md 第 3、6 条铁律）。

真源优先级（**只用本地真源，不联网核实**）：
  ① 本店在架快照 `{vendorCode: row}` 的 nmId 字段（最准，含该店真实在架商品）
  ② 映射总表 state 的 nmId 字段（跨店兜底：vc 为跨店唯一键，商品可能在别的店在架）
两处都没有 → 调用方以 MISS_LABEL（「本地真源未收录」）标注。
"""
from typing import Any, Dict

from .. import common
from .mapping_repo import MappingRepository
from .product_repo import ProductSnapshotRepository

MISS_LABEL = "本地真源未收录"   # nmId 在本店快照与映射表中均查不到（不联网核实）


class NmResolver:
    """按店本地反查 nmId → 供应商代码 / 中文名（一次读快照 + 映射池，失败降级并打警告）"""

    def __init__(self, shop_id: Any, with_cn: bool = True):
        self.nm2vc: Dict[int, str] = {}
        self.nm2src: Dict[int, str] = {}
        self._cn = (lambda vc: "")
        try:
            resolve_cn, _ = MappingRepository.build_vc_resolver()
            for vc, item in (ProductSnapshotRepository.load_shop_rows(shop_id) or {}).items():
                nm_id = common.to_int(item.get("nmId") or item.get("nmID"))
                if nm_id:
                    self.nm2vc[nm_id] = vc
                    self.nm2src[nm_id] = "店快照"
            state, _ = MappingRepository.load_mapping_state()
            for vc, info in state.items():
                nm_id = common.to_int((info or {}).get("nmId"))
                if nm_id and nm_id not in self.nm2vc:
                    self.nm2vc[nm_id] = vc
                    self.nm2src[nm_id] = "映射表"
            if with_cn:
                self._cn = lambda vc: resolve_cn(vc, "")
        except Exception as e:
            # 铁律 9：降级必须打 [警告]，不静默吞异常
            print(f"  [警告] 本地反查真源加载失败（{type(e).__name__}: {e}）；"
                  f"供应商代码/中文名将全部标注「{MISS_LABEL}」")

    def resolve(self, nm_id: Any) -> Dict[str, str]:
        """nm_id → {'vc': 供应商代码, 'cn': 中文名, 'src': 解析来源}"""
        key = common.to_int(nm_id)
        vc = self.nm2vc.get(key, "")
        return {"vc": vc, "cn": self._cn(vc) if vc else "", "src": self.nm2src.get(key, "")}

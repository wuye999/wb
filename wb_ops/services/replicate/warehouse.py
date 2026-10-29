# -*- coding: utf-8 -*-
"""wb_ops 仓库 ID 解析（WB 原生唯一入口）

背景（2026-10-29）：仓库 ID 原先由 BCS `system/wbWarehouses/list` 提供，现改为
WB 原生 `portal/offices/stores/short`，**全项目只此一处解析**，避免三份重复实现
（原 shelve_common.main_warehouse / replicate.main_warehouse / ops_plan._default_warehouse）。

兜底链（只用 WB 原生，不再读 known_warehouses.json）：
    1) name 含 config.DEFAULT_WAREHOUSE_NAME（"莫斯科"）→ 用该条 id
    2) RE_REGION 命中 name 或 warehouseName（俄文）且 name 不含"成都"→ 用该条 id
    3) None（由调用方报错/跳过；**不降级到 BCS，也不读本地表**）

⚠ 取值一律用响应里的 `id`（= warehouseId 体系）；响应里的 `warehouseId` 是 officeId。
"""
import re
from typing import Any, Dict, List, Optional

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import wb_warehouse_client as wb_wh

# 俄罗斯地区仓关键词（莫斯科之外的备选主仓；成都仓=国内仓不参与）
RE_REGION = re.compile(r"подольск|электросталь|хоругвино|колчанино|восток|север|юг", re.I)
CHENGDU_NAME = "成都仓库"
DELIVERY_TYPE = 1          # 1 = FBS 直发（清库存/下架/上架均用该模式）

# 只缓存「成功解析到」的结果：失败不缓存，便于下一店/下一次重试
_cache: Dict[int, int] = {}


def _shop(sid: int):
    return credentials.get().wb_shop(int(sid))


def list_stores(sid: int) -> List[Dict[str, Any]]:
    """拉取该店 WB 仓库列表；失败/无凭证返回 []（并打印明确原因）。"""
    sid = int(sid)
    shop = _shop(sid)
    if not shop:
        print(f"  [错误] 店{sid} 无 WB 凭证，无法获取仓库列表"
              f"（credentials.json wb.shops 缺该店或三件套不齐）")
        return []
    try:
        return wb_wh.get_stores(shop, delivery_type=DELIVERY_TYPE)
    except common.CookieExpiredError as e:
        print(f"  [错误] 店{sid} WB cookie 失效，无法获取仓库列表：{e}")
    except Exception as e:
        print(f"  [错误] 店{sid} 获取仓库列表失败：{type(e).__name__}: {str(e)[:120]}")
    return []


def resolve_warehouse(sid: int) -> Optional[int]:
    """店铺默认主仓 ID（莫斯科 → 俄罗斯地区仓 → None）。带进程内缓存（仅成功值）。"""
    sid = int(sid)
    if sid in _cache:
        return _cache[sid]
    stores = list_stores(sid)
    if not stores:
        return None

    target = config.DEFAULT_WAREHOUSE_NAME
    for s in stores:
        if target and target in (s.get("name") or ""):
            _cache[sid] = int(s["id"])
            return _cache[sid]

    for s in stores:
        name = s.get("name") or ""
        if "成都" in name:
            continue
        if RE_REGION.search(name) or RE_REGION.search(s.get("warehouseName") or ""):
            _cache[sid] = int(s["id"])
            return _cache[sid]

    print(f"  [警告] 店{sid} 仓库列表中没有「{target}」或俄罗斯地区仓"
          f"（共 {len(stores)} 个：{[s.get('name') for s in stores]}）")
    return None


def chengdu_warehouses() -> List[Dict[str, Any]]:
    """全部店铺的成都仓 → [{'shopId','id','name'}]（供 remote-wh 等使用）。

    注意：`id` 是仓库/门店 ID（原 BCS 侧该字段同名），不是 officeId。
    """
    out: List[Dict[str, Any]] = []
    for shop in credentials.get().shops:
        sid = shop.get("shopId")
        for s in list_stores(sid):
            if str(s.get("name") or "").strip() == CHENGDU_NAME:
                out.append({"shopId": sid, "id": s.get("id"), "name": s.get("name")})
    return out


def clear_cache() -> None:
    """清空解析缓存（测试 / 手动刷新用）。"""
    _cache.clear()

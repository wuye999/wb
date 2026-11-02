"""sellerops 仓库 ID 解析（WB 原生唯一入口）

背景（2026-10-29）：仓库 ID 原先由 BCS `system/wbWarehouses/list` 提供，现改为
WB 原生 `portal/offices/stores/short`，**全项目只此一处解析**，避免三份重复实现
（原 publishing.main_warehouse / replicate.main_warehouse / ops_plan._default_warehouse）。

只接受唯一、可用且名称匹配「莫斯科」的仓库；缺失、歧义或处理中返回 None。
不回退其他地区仓，不降级到 BCS，也不读取本地旧仓库表。

⚠ 取值一律用响应里的 `id`（= warehouseId 体系）；响应里的 `warehouseId` 是 officeId。
"""
from typing import Any, Dict, List, Optional

from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.integrations.wildberries import warehouses as wb_wh

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
    """确认该店唯一可用的莫斯科仓库 ID；仅成功结果按店缓存。"""
    sid = int(sid)
    if sid in _cache:
        return _cache[sid]
    stores = list_stores(sid)
    if not stores:
        return None

    target = config.DEFAULT_WAREHOUSE_NAME
    matches = [s for s in stores if target and target in (s.get("name") or "")
               and "成都" not in (s.get("name") or "") and not s.get("isDeleting")]
    if len(matches) != 1 or matches[0].get("isProcessing"):
        print(f"  [警告] 店{sid} 无法确认唯一可用的「{target}」仓库，停止该店仓库操作；不回退其他仓库")
        return None
    raw_id = matches[0].get("id")
    try:
        warehouse_id = int(raw_id)
    except (TypeError, ValueError):
        warehouse_id = 0
    if isinstance(raw_id, bool) or warehouse_id <= 0 or str(raw_id).strip() != str(warehouse_id):
        print(f"  [警告] 店{sid} 莫斯科仓库 id 无效，停止该店仓库操作")
        return None
    _cache[sid] = warehouse_id
    return warehouse_id


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

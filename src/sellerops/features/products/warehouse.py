"""从本店 WB 后台获取仓库清单，缓存同次选择的 ID 和名称。"""
from typing import Any, Dict, List, Optional

from sellerops.domain.warehouses import select_warehouse
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.integrations.wildberries import warehouses as wb_wh

from functools import lru_cache
DELIVERY_TYPE = 1          # 1 = FBS 直发（清库存/下架/上架均用该模式）

# 只缓存「成功解析到」的结果：失败不缓存，便于下一店/下一次重试
@lru_cache(maxsize=1)
def _warehouse_details(data: config.DataPaths) -> dict[int, dict]:
    return {}


def warehouse_ids(data: config.DataPaths) -> dict[int, int]:
    return {sid: warehouse['id'] for sid, warehouse in _warehouse_details(data).items()}


def cached_warehouse_name(sid: int, warehouse_id: int) -> str | None:
    """仅返回与本次使用 ID 一致的仓库名称，无网络读取。"""
    selected = _warehouse_details(config.current).get(sid)
    return selected['name'] if selected and selected['id'] == warehouse_id else None


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
    """确认本店已配置的 WB 仓库 ID；仅成功结果按店缓存。"""
    sid = int(sid)
    cache = _warehouse_details(config.current)
    if sid in cache:
        return cache[sid]['id']
    keyword = credentials.get().wb_warehouse_keyword
    stores = list_stores(sid) if keyword else []
    try:
        selected = select_warehouse(stores, keyword)
    except ValueError as error:
        print(f"[阻止] 店{sid}：{error}")
        return None
    cache[sid] = selected
    return selected['id']


def clear_cache() -> None:
    """清空解析缓存（测试 / 手动刷新用）。"""
    _warehouse_details.cache_clear()

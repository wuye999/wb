"""从本店 WB 后台获取仓库清单，按本次运行缓存已确认的莫斯科仓 id。"""
from typing import Any, Dict, List, Optional

from sellerops.domain.warehouses import moscow_warehouse
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.integrations.wildberries import warehouses as wb_wh

from functools import lru_cache
DELIVERY_TYPE = 1          # 1 = FBS 直发（清库存/下架/上架均用该模式）

# 只缓存「成功解析到」的结果：失败不缓存，便于下一店/下一次重试
@lru_cache(maxsize=1)
def warehouse_ids(data: config.DataPaths) -> dict[int, int]:
    return {}


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
    cache = warehouse_ids(config.current)
    if sid in cache:
        return cache[sid]
    stores = list_stores(sid)
    if not stores:
        return None

    try:
        warehouse_id = moscow_warehouse(stores)["id"]
    except ValueError as error:
        print(f"[阻止] 店{sid}：{error}")
        return None
    cache[sid] = warehouse_id
    return warehouse_id


def clear_cache() -> None:
    """清空解析缓存（测试 / 手动刷新用）。"""
    warehouse_ids.cache_clear()

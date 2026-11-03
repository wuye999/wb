"""
sellerops BCS ERP 云端 API 客户端适配器 (BCSClient)
封装与 wb.bcserp.com 的全部 HTTP 通信：自动退避、限频处理、店铺列表、商品拉取、批量上品与同步。
"""
from sellerops.core.errors import NetworkError

import time
import requests
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.core.errors import AuthenticationError
from sellerops.core.errors import RateLimitError
from sellerops.core.errors import PlatformApiError
from sellerops.integrations.bcs.capture import BCS_BASE_URL, BCSSession

RETRY_MAX = 3
PAGE_SIZE_BIG = 10000


@dataclass(frozen=True)
class OpenAPIShop:
    """BCS 店铺的私有 Open API 访问资料；密钥不参与 repr。"""

    id: int
    name: str
    api_key: str = field(repr=False)


class BCSClient:
    """BCS ERP 客户端"""

    def __init__(self, session: BCSSession | None = None) -> None:
        self._session = session

    @property
    def base_url(self) -> str:
        return BCS_BASE_URL if self._session is not None else credentials.get().base_url

    def _headers(self) -> Dict[str, str]:
        return self._session.headers() if self._session is not None else credentials.get().bcs_headers()

    def request_json(self, method: str, url: str, retry: int = RETRY_MAX,
                     idempotent: bool = False, **kwargs) -> Dict[str, Any]:
        """Retry read requests; never replay ambiguous writes by default."""
        headers = self._headers()
        method = method.upper()
        if method == 'POST':
            headers['Content-Type'] = 'application/json;charset=UTF-8'
        attempts = max(1, retry) if method in {'GET', 'HEAD', 'OPTIONS'} or idempotent else 1
        for attempt in range(attempts):
            try:
                response = requests.request(method, url, headers=headers, timeout=60, **kwargs)
                if response.status_code in {401, 403}:
                    raise AuthenticationError('BCS 凭证无效或已过期')
                if response.status_code == 429:
                    if attempt + 1 < attempts:
                        time.sleep(3 * (attempt + 1))
                        continue
                    raise RateLimitError('BCS 请求受到限流')
                if response.status_code >= 500 and attempt + 1 < attempts:
                    time.sleep(2 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt + 1 == attempts:
                    raise NetworkError('BCS 网络请求失败') from exc
                time.sleep(2 * (attempt + 1))
            except requests.exceptions.HTTPError as exc:
                code = exc.response.status_code if exc.response is not None else None
                raise PlatformApiError('BCS HTTP 请求失败', status_code=code) from exc
            except requests.exceptions.JSONDecodeError as exc:
                raise PlatformApiError('BCS 返回非 JSON 数据') from exc
        raise NetworkError('BCS 请求重试耗尽')

    def get(self, url: str, retry: int = RETRY_MAX) -> Dict[str, Any]:
        return self.request_json("GET", url, retry)

    def post(self, url: str, data: Any, retry: int = RETRY_MAX) -> Dict[str, Any]:
        return self.request_json("POST", url, retry, json=data)

    def fetch_shop_list(self) -> List[Dict[str, Any]]:
        """账号全部店铺 → [{'id', 'name'}]"""
        url = f"{self.base_url}/system/wbShop/user/list?pageNum=1&pageSize=100"
        d = self.get(url)
        if d.get("code") != 200:
            raise PlatformApiError(f"获取店铺列表失败 code={d.get('code')} msg={d.get('msg')}")
        return [{"id": r.get("id"), "name": r.get("shopName")}
                for r in (d.get("rows") or []) if r.get("id")]

    def get_main_shop(self) -> int:
        if config.MAIN_SHOP:
            return config.MAIN_SHOP
        shops = self.fetch_shop_list()
        if not shops:
            raise PlatformApiError("未获取到店铺列表")
        return shops[0]["id"]

    def fetch_open_api_shops(self) -> List[OpenAPIShop]:
        """完整分页读取店铺访问资料，供显式 Open API 集成内部使用，不打印原始行。"""
        shops: List[OpenAPIShop] = []
        seen = set()
        for page in range(1, 1001):
            d = self.get(f"{self.base_url}/system/wbShop/user/list?pageNum={page}&pageSize=100")
            if not isinstance(d, dict) or d.get('code') != 200 or not isinstance(d.get('rows'), list):
                raise PlatformApiError('BCS 店铺列表响应不完整')
            rows = d['rows']
            for row in rows:
                raw_id = row.get('id') if isinstance(row, dict) else None
                if isinstance(raw_id, bool) or not str(raw_id).isdigit() or int(raw_id) <= 0:
                    raise PlatformApiError('BCS 店铺列表含无效 ID')
                sid = int(raw_id)
                if sid in seen:
                    raise PlatformApiError('BCS 店铺列表分页重复，无法确认完整性')
                seen.add(sid)
                shops.append(OpenAPIShop(sid, str(row.get('shopName') or sid),
                                         str(row.get('apiKey') or '').strip()))
            total = d.get('total')
            if total is not None:
                if isinstance(total, bool) or not str(total).isdigit() or int(total) < len(shops):
                    raise PlatformApiError('BCS 店铺列表 total 无效或不一致')
                if len(shops) == int(total):
                    return shops
                if not rows:
                    raise PlatformApiError('BCS 店铺列表提前结束')
            elif not rows:
                return shops
        raise PlatformApiError('BCS 店铺列表超过分页上限')

    def fetch_shop_products(self, shop_id: int, filter_type: str = "BASE") -> List[Dict[str, Any]]:
        url = (f"{self.base_url}/shopKeeper/productList/list?filter={filter_type}"
               f"&pageNum=1&pageSize={PAGE_SIZE_BIG}&shopId={shop_id}")
        d = self.get(url)
        if not isinstance(d, dict) or d.get('code') != 200:
            raise PlatformApiError(f'店铺{shop_id} BCS 商品查询失败')
        rows = d.get('rows')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PlatformApiError(f'店铺{shop_id} BCS 商品响应缺少有效 rows，不能视为空店')
        total = d.get('total')
        if total is not None:
            if (isinstance(total, bool) or not str(total).isdecimal()
                    or int(total) != len(rows)):
                raise PlatformApiError(f'店铺{shop_id} BCS 商品总数与返回行数不一致，快照不完整')
        elif len(rows) >= PAGE_SIZE_BIG:
            raise PlatformApiError(f'店铺{shop_id} BCS 商品达到单页上限，无法确认完整性')
        if any(row.get('shopId') is not None and str(row['shopId']) != str(shop_id) for row in rows):
            raise PlatformApiError(f'店铺{shop_id} BCS 商品响应包含其他店铺数据')
        return rows

    def count_by_filter(self, shop_id: int) -> Dict[str, Any]:
        d = self.get(f"{self.base_url}/shopKeeper/productList/countByFilter?shopId={shop_id}")
        if d.get("code") != 200:
            raise PlatformApiError(f"countByFilter 失败 code={d.get('code')}")
        return d.get("data") or {}

    # 2026-10-29：fetch_warehouses() / default_warehouse_id() 已删除。
    # 仓库 ID 统一改由 WB 原生接口获取（adapters/wb_warehouse_client.py +
    # services/replicate/warehouse.py），不再依赖 BCS。

    def remove_to_trash(self, shop_id: int, nm_ids: List[int]) -> Dict[str, Any]:
        return self.post(f"{self.base_url}/shopKeeper/clean/removeToTrash",
                         [{"shopId": shop_id, "nmId": nm} for nm in nm_ids])

    def batch_push_products(self, shop_configs: List[Dict[str, Any]], sku_prices: List[Dict[str, Any]], mode: int = 1, carry_brand: int = 1) -> Dict[str, Any]:
        url = f"{self.base_url}/products/batch/push"
        body = {
            "shop": shop_configs,
            "mode": mode,
            "carryBrand": carry_brand,
            "customBrand": None,
            "vendorCodePrefix": None,
            "titleSuffix": None,
            "aiRewrite": False,
            "aiRetouch": False,
            "aiRetouchTemplateId": None,
            "imgUploadMode": 0,
            "skuPrices": sku_prices,
            "wbCollection": {},
        }
        return self.post(url, body)

    def sync_shop(self, shop_id: int, filter_type: str = "ALL") -> str:
        task_id = f"TASK_{int(time.time() * 1000)}"
        url = f"{self.base_url}/shopKeeper/product/sync?taskId={task_id}&shopId={shop_id}&filter={filter_type}"
        d = self.get(url)
        if d.get("code") != 200:
            raise PlatformApiError(f"触发同步失败 shop={shop_id} code={d.get('code')} msg={d.get('msg')}")
        return task_id

    def wait_sync_done(self, task_id: str, shop_id: Optional[int] = None, timeout: int = 600, interval: float = 2.5, quiet: bool = False):
        start = time.time()
        while time.time() - start < timeout:
            d = self.get(f"{self.base_url}/shopKeeper/product/sync/progress/{task_id}")
            data = d.get("data") or {}
            pct = data.get("completedCount")
            if not quiet and pct is not None:
                print(f"  [同步 店{shop_id}] {pct}%", end="\r" if pct < 100 else "\n", flush=True)
            if data.get("status") == 1:
                return
            time.sleep(interval)
        raise PlatformApiError(f"同步超时 task={task_id}")


client = BCSClient()
base_url = lambda: client.base_url
http_get_json = client.get
http_post_json = client.post
fetch_shop_list = client.fetch_shop_list
get_main_shop = client.get_main_shop
fetch_shop_products = client.fetch_shop_products
count_by_filter = client.count_by_filter
remove_to_trash = client.remove_to_trash
batch_push_products = client.batch_push_products
sync_shop = client.sync_shop
wait_sync_done = client.wait_sync_done


def print_shops(args=None):
    """打印全部店铺列表"""
    for s in fetch_shop_list():
        print(f"{s['id']} | {s['name']}")
    return 0


def sync_shops_parallel(shop_ids: list[int], workers: int = 2) -> None:
    """Synchronize explicitly requested shops with bounded concurrency."""
    from concurrent.futures import ThreadPoolExecutor

    def synchronize(shop_id: int) -> None:
        task_id = client.sync_shop(shop_id)
        client.wait_sync_done(task_id, shop_id=shop_id)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        list(pool.map(synchronize, shop_ids))

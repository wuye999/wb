# -*- coding: utf-8 -*-
"""
wb_ops BCS ERP 云端 API 客户端适配器 (BCSClient)
封装与 wb.bcserp.com 的全部 HTTP 通信：自动退避、限频处理、店铺列表、商品拉取、批量上品与同步。
"""
import time
import requests
from typing import Dict, Any, List, Optional
from .. import config
from .. import credentials
from ..framework.exceptions import AuthenticationError, RateLimitError, PlatformApiError

RETRY_MAX = 3
PAGE_SIZE_BIG = 10000


class BCSClient:
    """BCS ERP 客户端"""

    def __init__(self):
        pass

    @property
    def base_url(self) -> str:
        return credentials.get().base_url

    def _headers(self) -> Dict[str, str]:
        return credentials.get().bcs_headers()

    def request_json(self, method: str, url: str, retry: int = RETRY_MAX, **kwargs) -> Dict[str, Any]:
        """统一 HTTP 请求：429 指数退避重试；401 明确报错；返回 JSON dict。"""
        headers = self._headers()
        if method == "POST":
            headers["Content-Type"] = "application/json;charset=UTF-8"
        last = None
        for attempt in range(retry):
            try:
                resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
                if resp.status_code == 429:
                    if attempt < retry - 1:
                        wait = 3 * (attempt + 1)
                        print(f"    [429 限流] 等待 {wait}s 重试...")
                        time.sleep(wait)
                        continue
                    raise RateLimitError("BCS 接口调用触发 429 限频")
                if resp.status_code == 401:
                    raise AuthenticationError("BCS token 可能已过期，请更新 credentials.json 的 bcs.token")
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                last = e
                if attempt < retry - 1:
                    time.sleep(2)
                    continue
                code = e.response.status_code if e.response is not None else "?"
                raise PlatformApiError(f"HTTP {code}: {e}", status_code=code)
            except Exception as e:
                last = e
                if attempt < retry - 1:
                    time.sleep(2)
                    continue
        raise last if last else PlatformApiError("BCS 接口网络错误")

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

    def fetch_shop_products(self, shop_id: int, filter_type: str = "BASE") -> List[Dict[str, Any]]:
        url = (f"{self.base_url}/shopKeeper/productList/list?filter={filter_type}"
               f"&pageNum=1&pageSize={PAGE_SIZE_BIG}&shopId={shop_id}")
        d = self.get(url)
        if d.get("code") != 200:
            raise PlatformApiError(f"店铺{shop_id}拉取失败 code={d.get('code')} msg={d.get('msg')}")
        return d.get("rows") or []

    def count_by_filter(self, shop_id: int) -> Dict[str, Any]:
        d = self.get(f"{self.base_url}/shopKeeper/productList/countByFilter?shopId={shop_id}")
        if d.get("code") != 200:
            raise PlatformApiError(f"countByFilter 失败 code={d.get('code')}")
        return d.get("data") or {}

    def fetch_warehouses(self, shop_id: int) -> List[Dict[str, Any]]:
        d = self.get(f"{self.base_url}/system/wbWarehouses/list?shopId={shop_id}")
        if isinstance(d, list):
            return d
        return d.get("data") or d.get("rows") or []

    def default_warehouse_id(self, shop_id: int) -> Optional[int]:
        for w in self.fetch_warehouses(shop_id):
            if config.DEFAULT_WAREHOUSE_NAME in (w.get("name") or ""):
                return w.get("id")
        return None

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


# 全局单例与向后兼容导出
client = BCSClient()
base_url = lambda: client.base_url
http_get_json = client.get
http_post_json = client.post
fetch_shop_list = client.fetch_shop_list
get_main_shop = client.get_main_shop
fetch_shop_products = client.fetch_shop_products
count_by_filter = client.count_by_filter
fetch_warehouses = client.fetch_warehouses
default_warehouse_id = client.default_warehouse_id
remove_to_trash = client.remove_to_trash
batch_push_products = client.batch_push_products
sync_shop = client.sync_shop
wait_sync_done = client.wait_sync_done


def print_shops(args=None):
    """打印全部店铺列表"""
    for s in fetch_shop_list():
        print(f"{s['id']} | {s['name']}")
    return 0


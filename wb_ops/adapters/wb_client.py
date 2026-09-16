# -*- coding: utf-8 -*-
"""
wb_ops Wildberries 原生客户端适配器 (WBClient)
封装基于逆向会话的 WB 官方接口调用、Session 管理、重试、强类型领域模型转换与批量任务提交。
"""
import time
import requests
from typing import List, Dict, Any, Optional
from .. import credentials
from .. import common
from ..domain.models import TaskResult
from ..framework.exceptions import AuthenticationError, PlatformApiError

RETRY_SLEEPS = [1, 3, 8]


def make_session(shop: Dict[str, Any], root_version: Optional[str] = None) -> requests.Session:
    """按店铺构建 requests.Session（cookie + 三个业务头）。"""
    root_version = root_version or credentials.get().root_version
    s = requests.Session()
    for c in (shop.get("cookie") or "").split(";"):
        c = c.strip()
        if "=" in c:
            k, v = c.split("=", 1)
            s.cookies.set(k.strip(), v.strip())
    s.headers.update({
        "Accept": "*/*",
        "authorizev3": shop["authorizev3"],
        "wb-seller-lk": shop["wb_seller_lk"],
        "seller-lk": shop["wb_seller_lk"],
        "Content-Type": "application/json",
        "Origin": "https://seller.wildberries.ru",
        "Referer": "https://seller.wildberries.ru/",
        "root-version": root_version,
        "User-Agent": common.UA,
    })
    return s


def request(session: requests.Session, method: str, url: str, **kwargs) -> Any:
    """WB 接口统一请求：403 抛 CookieExpiredError；其他 4xx/5xx 指数退避重试。"""
    last = None
    for i, wait in enumerate([0] + RETRY_SLEEPS):
        try:
            r = session.request(method, url, timeout=30, **kwargs)
            if r.status_code == 403:
                raise common.CookieExpiredError("403：cookie 已失效（cfidsw-wb 过期），请刷新 credentials.json 该店 cookie")
            if r.status_code >= 400:
                raise PlatformApiError(f"HTTP {r.status_code}: {r.text[:200]}", status_code=r.status_code)
            return r.json()
        except (common.CookieExpiredError, AuthenticationError):
            raise
        except Exception as e:
            last = e
            if wait:
                print(f"    [重试] 等待 {wait}s ...")
                time.sleep(wait)
    raise last


def request_post(session: requests.Session, url: str, payload: Any, allow_400_json: bool = False) -> Any:
    """POST 专用请求。"""
    last = None
    for i, wait in enumerate([0] + RETRY_SLEEPS):
        try:
            r = session.post(url, json=payload, timeout=30)
            if r.status_code == 403:
                raise common.CookieExpiredError("403：cookie 已失效（cfidsw-wb 过期），请刷新 credentials.json 该店 cookie")
            if r.status_code == 400 and allow_400_json:
                try:
                    return r.json()
                except Exception:
                    raise PlatformApiError(f"HTTP 400: {r.text[:200]}", status_code=400)
            if r.status_code >= 400:
                raise PlatformApiError(f"HTTP {r.status_code}: {r.text[:200]}", status_code=r.status_code)
            return r.json()
        except (common.CookieExpiredError, AuthenticationError):
            raise
        except Exception as e:
            last = e
            if wait:
                print(f"    [重试] 等待 {wait}s ...")
                time.sleep(wait)
    raise last


class WBClient:
    """Wildberries 官方 Web 接口封装"""

    DISC_LIST = "https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/list/goods/filter"
    DISC_UPLOAD = "https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/upload/task?checkChange=true"

    def __init__(self, shop_dict: Dict[str, Any], root_version: Optional[str] = None):
        self.shop_dict = shop_dict
        self.shop_id = shop_dict.get("shopId") or shop_dict.get("shop_id")
        self.shop_name = shop_dict.get("shopName") or shop_dict.get("shop_name") or f"shop_{self.shop_id}"
        self.session = make_session(shop_dict, root_version)

    def fetch_discount_goods_desc(
        self,
        threshold: int,
        limit: int = 0,
        page_size: int = 100,
        max_pages: int = 200,
    ) -> List[Dict[str, Any]]:
        """从高到低分页拉取折扣商品，遇到首条 <= threshold 提前截断。"""
        items: List[Dict[str, Any]] = []
        offset = 0
        pages = 0

        while pages < max_pages:
            body = {
                "limit": page_size,
                "offset": offset,
                "facets": [],
                "filterWithoutPrice": False,
                "filterWithLeftovers": False,
                "filterWithoutCompetitivePrice": False,
                "sort": "discount",
                "sortOrder": 0,
            }
            res = request(self.session, "POST", self.DISC_LIST, json=body)
            goods = (res.get("data") or {}).get("listGoods") or []
            pages += 1
            if not goods:
                break

            first_d = goods[0].get("discount")
            if threshold >= 0 and first_d is not None and int(first_d) <= threshold:
                break

            for g in goods:
                disc = g.get("discount")
                if threshold >= 0 and (disc is None or int(disc) <= threshold):
                    continue
                items.append(g)
                if limit and len(items) >= limit:
                    break

            if limit and len(items) >= limit:
                break
            if len(goods) < page_size:
                break

            offset += page_size
            time.sleep(0.15)

        return items

    def upload_batch_discount(self, data_payload: List[Dict[str, Any]]) -> TaskResult:
        """分批提交 WB 原生改折扣任务。"""
        if not data_payload:
            return TaskResult(success=True, processed_count=0, shop_id=self.shop_id)

        try:
            res = request(self.session, "POST", self.DISC_UPLOAD, json={"data": data_payload})
            err = res.get("error")
            err_text = res.get("errorText") or ""
            r_data = res.get("data") or {}
            task_id = r_data.get("id")

            if err:
                return TaskResult(
                    task_id=task_id,
                    success=False,
                    processed_count=0,
                    error_message=err_text or "WB 返回未知错误",
                    shop_id=self.shop_id,
                )

            return TaskResult(
                task_id=task_id,
                success=True,
                processed_count=len(data_payload),
                shop_id=self.shop_id,
            )
        except Exception as e:
            return TaskResult(
                success=False,
                processed_count=0,
                error_message=str(e),
                shop_id=self.shop_id,
            )


CANCELED_URL = ("https://marketplace.wildberries.ru/ns/marketplace-app/"
                "marketplace-remote-wh/api/v3/portal/fbs/orders/canceled")


def fetch_canceled_ids(shop_id: int, max_pages: int = 10) -> set:
    """查询该店已取消订单的 WB 平台单号集合（2026-09-09 抓包实测）。
    GET portal/fbs/orders/canceled?order=desc&type=fbs&next=<游标>
    鉴权：WB 三件套（make_session）；翻页跟随 data.next，
    游标不变或超 max_pages 终止防死循环。失败抛异常由调用方降级。"""
    cred = credentials.get()
    shop = next((s for s in cred.wb_shops()
                 if int(s.get("shopId", 0)) == int(shop_id)), None)
    if not shop:
        raise RuntimeError(f"credentials.json wb.shops 中无店铺 {shop_id}")
    session = make_session(shop, cred.root_version)
    ids, nxt, page, seen = set(), 0, 0, None
    while True:
        params = {"order": "desc", "type": "fbs", "next": nxt}
        d = request(session, "GET", CANCELED_URL, params=params)
        data = d.get("data") or {}
        for o in data.get("orders") or []:
            if o.get("id"):
                ids.add(str(o["id"]))
        page += 1
        nxt = data.get("next")
        if not nxt or str(nxt) == str(seen) or page >= max_pages:
            break
        seen = nxt
    return ids


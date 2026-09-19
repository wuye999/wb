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
from ..domain.models import DiscountUploadResult
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
    DISC_UPLOAD = "https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/upload/task"

    @classmethod
    def _disc_upload_url(cls, check_change: bool) -> str:
        """upload/task 的 URL 构造：checkChange=true（预检）/ false（真正提交）。"""
        return f"{cls.DISC_UPLOAD}?checkChange={'true' if check_change else 'false'}"

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
        """从高到低分页拉取折扣商品，遇到首条 <= threshold 提前截断（返回折扣 > threshold）。"""
        return self._fetch_discount_goods(threshold, limit, page_size, max_pages, sort_order=0)

    def fetch_discount_goods_asc(
        self,
        threshold: int,
        limit: int = 0,
        page_size: int = 100,
        max_pages: int = 200,
    ) -> List[Dict[str, Any]]:
        """从低到高分页拉取折扣商品（sortOrder=1），遇到首条 >= threshold 提前截断（返回折扣 < threshold）。

        用于「折扣 < N」侧筛选：降序接口在首条 ≤ threshold 时即截断，永远覆盖不到低折扣区间。
        实测来源：抓包 `api/网络请求/wb折扣从小到大排序api.har`（body 仅 sortOrder 由 0 改 1）。
        """
        return self._fetch_discount_goods(threshold, limit, page_size, max_pages, sort_order=1)

    def _fetch_discount_goods(
        self,
        threshold: int,
        limit: int = 0,
        page_size: int = 100,
        max_pages: int = 200,
        sort_order: int = 0,
    ) -> List[Dict[str, Any]]:
        """按折扣排序分页拉取（sortOrder=0 降序取 >threshold / 1 升序取 <threshold）。

        提前截断：首条不满足条件即停（降序 = 首条 ≤ threshold；升序 = 首条 ≥ threshold）。
        """
        ascending = sort_order == 1
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
                "sortOrder": sort_order,
            }
            res = request(self.session, "POST", self.DISC_LIST, json=body)
            goods = (res.get("data") or {}).get("listGoods") or []
            pages += 1
            if not goods:
                break

            first_d = goods[0].get("discount")
            if threshold >= 0 and first_d is not None:
                if (ascending and int(first_d) >= threshold) or (not ascending and int(first_d) <= threshold):
                    break

            for g in goods:
                disc = g.get("discount")
                if threshold >= 0:
                    if disc is None:
                        continue
                    if ascending and int(disc) >= threshold:
                        continue
                    if not ascending and int(disc) <= threshold:
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

    def precheck_batch_discount(self, data_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        """提交前预检（checkChange=true）：判定是否触发「降价提示 / 隔离区提示」弹窗。

        Returns:
            {"priceModal": bool, "quarantineModal": bool, "error": bool, "errorText": str}；
            网络/解析异常直接抛出，由调用方决定是否降级提交。
        """
        res = request(self.session, "POST", self._disc_upload_url(check_change=True),
                      json={"data": data_payload})
        r_data = res.get("data") or {}
        return {
            "priceModal": bool(r_data.get("priceModal")),
            "quarantineModal": bool(r_data.get("quarantineModal")),
            "error": bool(res.get("error")),
            "errorText": res.get("errorText") or "",
        }

    def upload_batch_discount(
        self,
        data_payload: List[Dict[str, Any]],
        precheck: bool = True,
    ) -> DiscountUploadResult:
        """分批提交 WB 原生改折扣任务（两阶段：checkChange=true 预检 → checkChange=false 提交）。

        实测（2026-09-17 抓包 `api/网络请求/wb批量修改折扣+降价提示.har`）：
        1. `POST .../upload/task?checkChange=true` 只做**预检不落库**，仅返回
           `{"data":{"priceModal":bool,"quarantineModal":bool}}`（无 id）；降幅较大时
           WB 会要求先弹「降价提示 / 隔离区提示」，用户确认后才提交。
        2. `POST .../upload/task?checkChange=false` 才是**真正提交**，返回
           `{"data":{"id":<taskId>,"alreadyExists":bool}}`。

        ⚠ 旧实现 URL 写死 `checkChange=true`，导致每次都停在预检阶段（响应里没有 id、
        taskId 恒为 None），平台侧实际未落库 —— 这也是「改折扣没生效」的根因。

        Args:
            data_payload: 待提交记录列表（vendorCode / nmID / discount / currencyIsoCode）。
            precheck: 是否先做预检（默认 True）。预检异常时降级直连提交，不阻断业务。

        Returns:
            DiscountUploadResult：含 task_id（真实提交的任务号）、是否已存在、
            以及预检返回的 price/quarantine 弹窗标记。
        """
        if not data_payload:
            return DiscountUploadResult(success=True, processed_count=0, shop_id=self.shop_id)

        price_modal = False
        quarantine_modal = False

        if precheck:
            try:
                pre = self.precheck_batch_discount(data_payload)
                price_modal = pre["priceModal"]
                quarantine_modal = pre["quarantineModal"]
                if pre.get("error"):
                    return DiscountUploadResult(
                        success=False,
                        processed_count=0,
                        error_message=pre.get("errorText") or "WB 预检返回未知错误",
                        shop_id=self.shop_id,
                        price_modal=price_modal,
                        quarantine_modal=quarantine_modal,
                    )
            except Exception as e:
                # 预检失败不阻断：预检仅是弹窗判定，降级直连提交（与平台「成功返回即生效」一致）
                print(f"    [预检降级] {e}（跳过预检，直接提交）")

        try:
            res = request(self.session, "POST", self._disc_upload_url(check_change=False),
                          json={"data": data_payload})
            err = res.get("error")
            err_text = res.get("errorText") or ""
            r_data = res.get("data") or {}
            task_id = r_data.get("id")
            already_exists = bool(r_data.get("alreadyExists"))

            if err:
                return DiscountUploadResult(
                    task_id=task_id,
                    success=False,
                    processed_count=0,
                    error_message=err_text or "WB 返回未知错误",
                    shop_id=self.shop_id,
                    already_exists=already_exists,
                    price_modal=price_modal,
                    quarantine_modal=quarantine_modal,
                )

            return DiscountUploadResult(
                task_id=task_id,
                success=True,
                processed_count=len(data_payload),
                shop_id=self.shop_id,
                already_exists=already_exists,
                price_modal=price_modal,
                quarantine_modal=quarantine_modal,
            )
        except Exception as e:
            return DiscountUploadResult(
                success=False,
                processed_count=0,
                error_message=str(e),
                shop_id=self.shop_id,
                price_modal=price_modal,
                quarantine_modal=quarantine_modal,
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


# basket 号查找表（权威来源对齐 批量上架/src/45-api-wb.js 与插件 1.2.5 NM_BASKET_SHARD_LIMITS 52 个区间）
# 47~52 及后续以步长 768 递增；53~60 预置并结合动态步长外推，防止超出已知表时 404
_BASKET_TABLE = [
    (143, "01"), (287, "02"), (431, "03"), (719, "04"), (1007, "05"), (1061, "06"),
    (1115, "07"), (1169, "08"), (1313, "09"), (1601, "10"), (1655, "11"), (1919, "12"),
    (2045, "13"), (2189, "14"), (2405, "15"), (2621, "16"), (2837, "17"), (3053, "18"),
    (3269, "19"), (3485, "20"), (3701, "21"), (3917, "22"), (4133, "23"), (4349, "24"),
    (4565, "25"), (4877, "26"), (5189, "27"), (5501, "28"), (5813, "29"), (6125, "30"),
    (6437, "31"), (6749, "32"), (7061, "33"), (7373, "34"), (7685, "35"), (7997, "36"),
    (8309, "37"), (8741, "38"), (9173, "39"), (9605, "40"), (10373, "41"), (11141, "42"),
    (11909, "43"), (12677, "44"), (13445, "45"), (14213, "46"), (14981, "47"), (15749, "48"),
    (16517, "49"), (17285, "50"), (18053, "51"), (18821, "52"), (19589, "53"), (20357, "54"),
    (21125, "55"), (21893, "56"), (22661, "57"), (23429, "58"), (24197, "59"), (24965, "60"),
]


def basket_base(nm_id):
    """nmId → basket CDN 基础路径（card.json 基于它）"""
    n = int(nm_id)
    vol, part = n // 100000, n // 1000
    basket = None
    for threshold, no in _BASKET_TABLE:
        if vol <= threshold:
            basket = no
            break
    if not basket:
        # 超过已知表格时按 WB 标准步长 768 动态外推（对齐 45-api-wb.js 的 768 步长）
        last_thresh, last_no = _BASKET_TABLE[-1]
        b_num = int(last_no) + max(1, (vol - last_thresh + 767) // 768)
        basket = f"{b_num:02d}"
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{n}"


def fetch_card_json(nm_id):
    """basket CDN card.json → dict；失败返回 None。"""
    url = f"{basket_base(nm_id)}/info/ru/card.json"
    try:
        resp = requests.get(url, headers={"User-Agent": common.UA}, timeout=30)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def card_color_names(card):
    """从 card.json 提取颜色名。"""
    colors = card.get("colors") or []
    nm_names = card.get("nm_colors_names")
    if isinstance(nm_names, str):
        nm_names = [nm_names]
    names = []
    for i, item in enumerate(colors):
        if isinstance(item, dict):
            n = item.get("name")
        elif isinstance(nm_names, list) and i < len(nm_names):
            n = nm_names[i]
        else:
            n = None
        if n:
            names.append(n)
    return "、".join(names)


def fetch_product_info(nm_id, vc="", own=None):
    """整合 WB 商品基础信息（标题、品牌、颜色、售价、选项参数等）。供客服与监控模块调用。"""
    info = {"title": "", "brand": "", "colors": "", "price": "", "description": "", "options": ""}
    card = fetch_card_json(nm_id)
    if card:
        info["title"] = card.get("imt_name") or ""
        desc = (card.get("description") or "").strip()
        info["description"] = desc[:400] + ("…" if len(desc) > 400 else "")
        opts_str = "；".join(
            f"{o.get('name')}: {o.get('value')}" for o in (card.get("options") or [])
            if o.get("name") and o.get("value"))
        info["options"] = opts_str[:600] + ("…" if len(opts_str) > 600 else "")
        info["colors"] = card_color_names(card)
    if own is None:
        try:
            from wb_ops.storage.mapping_repo import MappingRepository
            own = MappingRepository.load_mapping_state()[0]
        except Exception:
            own = {}
    row = (own or {}).get(vc or "")
    if row:
        price_v = None
        sp = row.get("shop_price")
        if sp not in (None, ""):
            price_v = float(sp)
        elif row.get("dp") is not None:
            price_v = float(row["dp"])
        if price_v is not None:
            disc = common.to_int(row.get("discount")) if row.get("discount") not in (None, "") else 0
            if disc:
                price_v = price_v * (100 - disc) / 100
            info["price"] = f"{price_v:.0f} CNY"
    return info



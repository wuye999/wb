"""
sellerops Wildberries 原生客户端适配器 (WBClient)
封装基于逆向会话的 WB 官方接口调用、Session 管理、重试、强类型领域模型转换与批量任务提交。
"""
import time
import requests
from typing import List, Dict, Any, Optional
from sellerops.settings import credentials
from sellerops.core import utils as common
from sellerops.domain.models import DiscountUploadResult
from sellerops.core.errors import AuthenticationError
from sellerops.core.errors import PlatformApiError

RETRY_SLEEPS = [1, 3, 8]
QUESTIONS_URL = "https://seller-reviews.wildberries.ru/ns/fa-seller-api/reviews-ext-seller-portal/api/v2/questions"
_UPDATE_SESSION = "请用一店的新抓包运行 sellerops cookies-update <抓包文件>"


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


def _check_auth(status: int) -> None:
    if status == 401:
        raise AuthenticationError(f"WB 401：卖家后台会话失效，{_UPDATE_SESSION}")
    if status == 403:
        raise common.CookieExpiredError(f"WB 403：卖家后台请求被拒绝，{_UPDATE_SESSION}")


def request(session: requests.Session, method: str, url: str, **kwargs) -> Any:
    """WB 接口统一请求。

    401/403 立即抛出认证异常，由用户显式更新凭证；
    其他 4xx/5xx 指数退避重试。
    """
    last = None
    for wait in [0] + RETRY_SLEEPS:
        try:
            r = session.request(method, url, timeout=30, **kwargs)
            _check_auth(r.status_code)
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
    for wait in [0] + RETRY_SLEEPS:
        try:
            r = session.post(url, json=payload, timeout=30)
            _check_auth(r.status_code)
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

        此端点属于已授权写入流程。平台也可能在该阶段返回任务号，不能把它当作只读预览。

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

        1. `POST .../upload/task?checkChange=true` 请求预检，可能返回
           `priceModal/quarantineModal`，也可能已经返回任务号；不能据此保证没有写入。
        2. `POST .../upload/task?checkChange=false` 确认提交，记录响应中的
           `id/alreadyExists`。平台可能对两阶段返回同一任务号，确认阶段以幂等已存在结束。

        两阶段都只在已授权提交时运行，不用于 dry-run；收到任务号仅表示受理，不能证明最终生效。

        Args:
            data_payload: 待提交记录列表（vendorCode / nmID / discount / currencyIsoCode）。
            precheck: 是否先做预检（默认 True）。认证失败结束本批，其他预检异常时降级直连提交。

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
            except (common.CookieExpiredError, AuthenticationError) as e:
                return DiscountUploadResult(success=False, error_message=str(e), shop_id=self.shop_id)
            except Exception as e:
                # 保留已授权写入的降级提交路径；最终按确认响应记录受理结果，不宣称即时生效。
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
    (25733, "61"), (26501, "62"), (27269, "63"), (28037, "64"), (28805, "65"),
]


def basket_base(nm_id):
    """nmId → basket CDN 基础路径（card.json / 图片基于它）。

    分片表对齐官方插件 1.3.4（权威来源：插件 sourcemap → wbBasketUrl.ts，65 个区间）；
    旧版只到 `24965→60` 并用 768 步长外推，vol > 24965 会算出并不存在的 61/62/… 分片。
    越界（vol > 28805）按官方 `resolveNmBasketBucket` 行为**回退到表尾分片**（65），不再外推。
    """
    n = int(nm_id)
    vol, part = n // 100000, n // 1000
    basket = _BASKET_TABLE[-1][1]          # 越界回退 = 表尾分片
    for threshold, no in _BASKET_TABLE:
        if vol <= threshold:
            basket = no
            break
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{n}"


CARD_RETRY = 3          # card.json 取用重试次数（CDN 偶发 ReadTimeout / SSLError / 边缘 404）
CARD_RETRY_DELAY = 1.5  # 重试间隔（秒）


def fetch_card_json(nm_id, retry: int = CARD_RETRY):
    """basket CDN card.json → dict；全部重试失败才返回 None。

    ⚠ 不要用「一次失败就 None」的写法：实测同一 URL 连测 5 次会出现
    `200/200/ReadTimeout/200/200`，单次超时会让 上架/图片 链路误判「卡不存在」
    （2026-10-19 排查 expectedSorting 时踩到）。重试覆盖 超时/连接错误/5xx/404 边缘缓存。
    """
    url = f"{basket_base(nm_id)}/info/ru/card.json"
    for attempt in range(max(1, int(retry))):
        try:
            resp = requests.get(url, headers={"User-Agent": common.UA}, timeout=30)
            if resp.status_code == 200:
                return resp.json()
        except Exception:                      # noqa: BLE001 —— 网络类异常统一重试
            pass
        if attempt < max(1, int(retry)) - 1:
            time.sleep(CARD_RETRY_DELAY)
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

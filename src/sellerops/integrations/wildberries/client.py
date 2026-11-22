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
from sellerops.domain.snapshot_values import positive_id, quantity
from sellerops.core.errors import AuthenticationError
from sellerops.core.errors import PlatformApiError

RETRY_SLEEPS = [1, 3, 8]
QUESTIONS_URL = "https://seller-reviews.wildberries.ru/ns/fa-seller-api/reviews-ext-seller-portal/api/v2/questions"
_UPDATE_SESSION = "请独立运行 sellerops login wb 更新凭证"


from functools import lru_cache
from sellerops.settings import paths


@lru_cache(maxsize=1)
def _stopped_shops(data: paths.DataPaths) -> dict:
    return {}


def make_session(shop: Dict[str, Any], root_version: Optional[str] = None) -> requests.Session:
    """按店铺构建 requests.Session（cookie + 三个业务头）。"""
    root_version = root_version or credentials.get().root_version
    s = requests.Session()
    s._sellerops_shop = shop.get("shopId") or shop.get("shop_id")
    s._sellerops_stopped = _stopped_shops(paths.current)
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
        raise common.CookieExpiredError(f"WB 401：卖家后台会话失效，{_UPDATE_SESSION}")
    if status == 403:
        raise common.CookieExpiredError(f"WB 403：卖家后台请求被拒绝，{_UPDATE_SESSION}")


def request(session: requests.Session, method: str, url: str, *, read_only: bool | None = None,
            allow_400_json: bool = False, **kwargs) -> Any:
    """只有业务读取可以重试。写入超时或回执无法解析时保留未知结果。"""
    shop_id = vars(session).get("_sellerops_shop")
    stopped = vars(session).get("_sellerops_stopped", {})
    failure = stopped.get(shop_id) or vars(session).get("_sellerops_auth_failure")
    if failure:
        raise common.CookieExpiredError(failure)
    read = method.upper() in {"GET", "HEAD"} if read_only is None else read_only
    delays = [0, *RETRY_SLEEPS] if read else [0]
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            response = session.request(method, url, timeout=30, allow_redirects=False, **kwargs)
            _check_auth(response.status_code)
            if response.status_code == 400 and allow_400_json:
                return response.json()
            if not 200 <= response.status_code < 300:
                raise PlatformApiError(f"WB HTTP {response.status_code}", status_code=response.status_code)
            return response.json()
        except AuthenticationError as error:
            session._sellerops_auth_failure = str(error)
            if shop_id is not None:
                stopped[shop_id] = str(error)
            raise
        except (requests.RequestException, ValueError, PlatformApiError) as error:
            if not read:
                raise PlatformApiError(f"提交结果未知（{type(error).__name__}），请求已停止") from error
            if attempt + 1 == len(delays):
                raise


def request_post(session: requests.Session, url: str, payload: Any, allow_400_json: bool = False,
                 *, read_only: bool = False) -> Any:
    return request(session, "POST", url, json=payload, read_only=read_only, allow_400_json=allow_400_json)


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

    def fetch_discount_goods_desc(self, threshold: int, limit: int = 0, page_size: int = 100,
                                  max_pages: int = 200, *, skip_discount: int | None = None) -> list[dict]:
        """读取严格高于阈值的商品；threshold=-1 读取全部。"""
        return self._fetch_discount_goods(threshold, limit, page_size, max_pages, 0, skip_discount)

    def fetch_discount_goods_asc(self, threshold: int, limit: int = 0, page_size: int = 100,
                                 max_pages: int = 200, *, skip_discount: int | None = None,
                                 seen_ids: set[int] | None = None) -> list[dict]:
        """读取严格低于阈值的商品，已由降序侧覆盖的商品不占配额。"""
        return self._fetch_discount_goods(threshold, limit, page_size, max_pages, 1, skip_discount, seen_ids)

    def _fetch_discount_goods(self, threshold: int, limit: int, page_size: int, max_pages: int,
                              sort_order: int, skip_discount: int | None,
                              seen_ids: set[int] | None = None) -> list[dict]:
        ascending = sort_order == 1
        items, seen = [], set()
        previous = None
        qualified = 0
        for page in range(max_pages):
            body = {
                "limit": page_size, "offset": page * page_size, "facets": [],
                "filterWithoutPrice": False, "filterWithLeftovers": False,
                "filterWithoutCompetitivePrice": False, "sort": "discount", "sortOrder": sort_order,
            }
            response = request(self.session, "POST", self.DISC_LIST, read_only=True, json=body)
            if not isinstance(response, dict):
                raise PlatformApiError("WB 折扣列表响应格式无效")
            if response.get("error", False) is not False or response.get("code") not in (None, 0, 200, "200"):
                raise PlatformApiError("WB 折扣列表返回业务错误")
            data = response.get("data")
            goods = data.get("listGoods") if isinstance(data, dict) else None
            if not isinstance(goods, list) or len(goods) > page_size:
                raise PlatformApiError("WB 折扣列表缺少有效 listGoods")
            checked = []
            for row in goods:
                nm_id = positive_id(row.get("nmID")) if isinstance(row, dict) else None
                discount = quantity(row.get("discount")) if nm_id else None
                if (nm_id is None or not isinstance(row.get("vendorCode"), str) or not row["vendorCode"].strip()
                        or discount is None or discount > 100):
                    raise PlatformApiError("WB 折扣列表包含无效商品标识或折扣")
                if nm_id in seen:
                    raise PlatformApiError("WB 折扣列表重复商品，无法确认翻页范围")
                if previous is not None and (discount < previous if ascending else discount > previous):
                    raise PlatformApiError("WB 折扣列表顺序不一致，无法确认阈值范围")
                previous = discount
                seen.add(nm_id)
                checked.append((nm_id, discount, row))
            for nm_id, discount, row in checked:
                if threshold >= 0 and (discount >= threshold if ascending else discount <= threshold):
                    return items
                if seen_ids and nm_id in seen_ids:
                    continue
                items.append({**row, "nmID": nm_id, "discount": discount})
                qualified += discount != skip_discount
                if limit and qualified >= limit:
                    return items
            if len(goods) < page_size:
                return items
            if page + 1 < max_pages:
                time.sleep(0.15)
        raise PlatformApiError("WB 折扣列表到达安全分页上限，店铺范围不完整")

    def precheck_batch_discount(self, data_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        """提交前预检（checkChange=true）：判定是否触发「降价提示 / 隔离区提示」弹窗。

        此端点属于已授权写入流程。平台也可能在该阶段返回任务号，不能把它当作只读预览。

        Returns:
            {"priceModal": bool, "quarantineModal": bool, "error": bool, "errorText": str}；
            网络或解析异常时中止本批并记录阶段。
        """
        res = request(self.session, "POST", self._disc_upload_url(check_change=True),
                      json={"data": data_payload})
        if (not isinstance(res, dict) or type(res.get("error")) is not bool
                or (res["error"] is False and not isinstance(res.get("data"), dict))):
            raise ValueError("预检回执缺少明确状态")
        r_data = res.get("data") if isinstance(res.get("data"), dict) else {}
        return {
            "priceModal": bool(r_data.get("priceModal")),
            "quarantineModal": bool(r_data.get("quarantineModal")),
            "error": bool(res.get("error")),
            "errorText": res.get("errorText") or "",
            "taskId": positive_id(r_data.get("id")),
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
            precheck: 是否先做预检（默认 True）。预检失败或结果未知时中止本批。

        Returns:
            DiscountUploadResult：含 task_id（真实提交的任务号）、是否已存在、
            以及预检返回的 price/quarantine 弹窗标记。
        """
        if not data_payload:
            return DiscountUploadResult(success=True, processed_count=0, shop_id=self.shop_id)

        price_modal = False
        quarantine_modal = False
        precheck_task_id = None

        if precheck:
            try:
                pre = self.precheck_batch_discount(data_payload)
                price_modal = pre["priceModal"]
                quarantine_modal = pre["quarantineModal"]
                precheck_task_id = pre.get("taskId")
                if pre.get("error"):
                    return DiscountUploadResult(
                        success=False,
                        processed_count=0,
                        error_message=pre.get("errorText") or "WB 预检返回未知错误",
                        shop_id=self.shop_id,
                        price_modal=price_modal,
                        quarantine_modal=quarantine_modal,
                    )
            except AuthenticationError:
                raise
            except Exception as error:
                return DiscountUploadResult(success=False, shop_id=self.shop_id, outcome="unknown",
                                            error_message=f"预检结果未知，已停止：{error}")

        try:
            res = request(self.session, "POST", self._disc_upload_url(check_change=False),
                          json={"data": data_payload})
            if not isinstance(res, dict) or type(res.get("error")) is not bool:
                raise ValueError("提交回执缺少明确状态")
            data = res.get("data")
            task_id = positive_id(data.get("id")) if isinstance(data, dict) else None
            already_exists = isinstance(data, dict) and data.get("alreadyExists") is True
            if res["error"]:
                outcome = "unknown" if precheck_task_id is not None else "failed"
                message = res.get("errorText") or "WB 拒绝提交"
            elif task_id is None:
                outcome = "unknown"
                message = "缺少受理任务号"
            else:
                outcome = "accepted"
                message = ""
            return DiscountUploadResult(task_id=task_id or precheck_task_id, success=outcome == "accepted",
                                        outcome=outcome, error_message=message,
                                        processed_count=len(data_payload) if outcome == "accepted" else 0,
                                        shop_id=self.shop_id, already_exists=already_exists,
                                        price_modal=price_modal, quarantine_modal=quarantine_modal)
        except AuthenticationError as error:
            if precheck_task_id is None:
                raise
            return DiscountUploadResult(task_id=precheck_task_id, success=False, outcome="unknown",
                                        error_message=str(error), shop_id=self.shop_id, auth_stopped=True)
        except Exception as error:
            return DiscountUploadResult(task_id=precheck_task_id, success=False, outcome="unknown",
                                        error_message=str(error), shop_id=self.shop_id,
                                        price_modal=price_modal, quarantine_modal=quarantine_modal)


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

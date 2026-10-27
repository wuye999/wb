# -*- coding: utf-8 -*-
"""
wb_ops WB 卖家后台「订单动态」适配器 (wb_order_feed_client)

接口（抓包来源：api/网络请求/wb订单动态 查询退货率.har，2026-10-27，袁州店）：

    POST https://seller-content.wildberries.ru/ns/orders-api/content-analytics/api/v1/order-feed/orders
      body: {"currentPeriod":{"start":"YYYY-MM-DD","end":"YYYY-MM-DD"},
             "subjectIDs":[],"brandNames":[],"nmIDs":[],"tagIDs":[],
             "isDefective":false,"orderBy":{"field":"order.updated","mode":"desc"},
             "timezone":"America/Los_Angeles","limit":50[,"cursor":"<data.pageInfo.cursor>"]}
      resp: {"error":false,"errorText":"","additionalErrors":{},
             "data":{"pageInfo":{"cursor":"..."},"orders":[{product:{...},order:{...}}]}}

实测要点（务必保留这些判断，勿"顺手优化"）：
- `cursor` 是**偏移量编码**：base64("<offset>:<YYYY-MM-DD HH:MM:SS>")，limit=50 时依次
  50/100/150。⇒ 「首页返回行数 < 请求 limit 且仍有 cursor」= 服务端把 limit 夹小了，
  必须自适应降级；**禁止**用 `len(chunk) < limit` 判「已到底」（会漏数据）。
- `currentPeriod` 实测按 **`order.updated`** 过滤，不是 created（2026-10-27 实证：3 天窗口
  返回 65 行，updated 全部在窗口内，created 仅 7 行在窗口内）⇒ 上层若要「按下单时间」统计，
  必须自己按 `created` 再过滤一遍。
- `order.created` / `order.updated` 带 timezone 对应偏移（实测 -07:00）⇒ `created[:10]`
  即该时区日期，上层据此做本地窗口复核，无需再做时区换算。
- `order.status` 实测 created / buyout / cancel / return（用户口径另含 returnDefective）；
  `cancelType` 仅在 cancel 出现。状态归类由上层 `classify_status` 决定。
- 一行 = 一件商品（订单 id 尾两段形如 ".0.0"）；同一订单可占多行，本模块**不去重**，
  是否按订单号聚合由上层决定。
- HTTP 200 但 `error: true` 属于业务错误，必须抛异常，**绝不能**当空页继续翻。

鉴权：WB 三件套会话（`wb_api.make_session`）。会话由调用方构建，本模块不建 session。
   ⚠ 必须走 `wb_api.request`（有 401 自动续期分支），**不要**用 `request_post`
   （它没有 401 分支，会把 401 当网络异常重试 4 次后失败）。
"""
import base64
import time
from typing import Any, Dict, Iterator, List, Optional

from wb_ops.adapters import wb_client as wb_api
from wb_ops.framework.exceptions import PlatformApiError

ORDER_FEED_URL = ("https://seller-content.wildberries.ru/ns/orders-api/"
                  "content-analytics/api/v1/order-feed/orders")
DEFAULT_TZ = "America/Los_Angeles"   # 抓包值（WB 订单动态页所用时区）
PAGE_SIZE = 50                       # 抓包实测 limit=50
MAX_PAGES = 200                      # 翻页安全上限（200 × 50 = 10000 行/店）


def build_payload(start: str, end: str, cursor: Optional[str] = None,
                  limit: int = PAGE_SIZE, timezone: str = DEFAULT_TZ) -> Dict[str, Any]:
    """构造请求体（字段与顺序对齐抓包）。"""
    body: Dict[str, Any] = {
        "currentPeriod": {"start": start, "end": end},
        "subjectIDs": [], "brandNames": [], "nmIDs": [], "tagIDs": [],
        "isDefective": False,
        "orderBy": {"field": "order.updated", "mode": "desc"},
        "timezone": timezone,
        "limit": int(limit),
    }
    if cursor:
        body["cursor"] = cursor
    return body


def cursor_offset(cursor: Any) -> Optional[int]:
    """cursor → 已消费偏移量；解不出返回 None（仅用于日志核对，不参与控制流）。"""
    s = str(cursor or "").strip()
    if not s or ":" not in s:
        return None
    try:
        head = s.split(":", 1)[0]
        raw = base64.b64decode(head + "=" * (-len(head) % 4)).decode("utf-8", "replace")
        return int(str(raw).split(":", 1)[0])
    except Exception:
        return None


def fetch_orders_page(session, start: str, end: str, cursor: Optional[str] = None,
                      limit: int = PAGE_SIZE, timezone: str = DEFAULT_TZ) -> Dict[str, Any]:
    """拉一页 → {'orders': [...], 'cursor': '...'}（原样返回接口结构，不改字段名）。

    Raises:
        common.CookieExpiredError: 403（cookie 失效，该店应整体中止）
        AuthenticationError: 401 且自动续期未恢复
        PlatformApiError: 4xx/5xx，或 HTTP 200 但 error=true
    """
    res = wb_api.request(session, "POST", ORDER_FEED_URL,
                         json=build_payload(start, end, cursor, limit, timezone))
    if not isinstance(res, dict):
        raise PlatformApiError(f"订单动态返回非对象：{type(res).__name__}")
    if res.get("error"):
        txt = str(res.get("errorText") or "").strip()
        raise PlatformApiError(f"订单动态业务错误：{txt or res!r}"[:300], raw_response=res)
    data = res.get("data") or {}
    page = data.get("pageInfo") or {}
    return {"orders": list(data.get("orders") or []), "cursor": page.get("cursor") or ""}


def iter_orders(session, start: str, end: str, limit: int = PAGE_SIZE,
                max_pages: int = MAX_PAGES, timezone: str = DEFAULT_TZ,
                max_rows: int = 0, sleep: float = 0.3,
                verbose: bool = False) -> Iterator[Dict[str, Any]]:
    """游标翻页生成器：逐行 yield `data.orders[]` 的原始元素。

    Args:
        limit: **期望**页大小。首页若返回行数 < limit 且仍有 cursor，说明服务端夹了上限，
               会自动降级为实际行数（并打印提示）。
        max_pages: 翻页安全上限。
        max_rows: >0 时取够 N 行即停（0=翻到底）。
        sleep: 页间间隔秒（0=不限速，测试用）。

    终止条件（五重，缺一不可）：空页 / cursor 假值 / cursor 与上页相同 / 超过 max_pages /
    达到 max_rows。**不**用「本页行数 < limit」判到底。
    """
    page_size = max(1, int(limit or PAGE_SIZE))
    nxt: Optional[str] = None
    seen: Any = None
    page = rows = 0
    while True:
        got = fetch_orders_page(session, start, end, nxt, page_size, timezone)
        chunk, cur = got["orders"], got["cursor"]
        page += 1
        rows += len(chunk)
        if verbose:
            off = cursor_offset(cur)
            tail = f"，下一页 offset={off}" if off is not None else ""
            print(f"    [分页 {page}] 本页 {len(chunk)} 行，累计 {rows} 行{tail}", flush=True)
        for item in chunk:
            yield item
        if max_rows and rows >= max_rows:
            if verbose:
                print(f"    [提示] 已达行数上限 {max_rows}，停止翻页", flush=True)
            return
        if not chunk:
            return
        if 0 < len(chunk) < page_size and cur:
            if verbose:
                print(f"    [提示] 服务端页大小上限={len(chunk)}（请求 limit={page_size}），已自适应", flush=True)
            page_size = len(chunk)
        if not cur or str(cur) == str(seen):
            return
        if page >= max_pages:
            print(f"    [警告] 翻页超过 max_pages={max_pages} 仍未结束，提前终止（该店结果可能不完整）",
                  flush=True)
            return
        seen = nxt = cur
        if sleep:
            time.sleep(sleep)


def fetch_all_orders(session, start: str, end: str, limit: int = PAGE_SIZE,
                     max_pages: int = MAX_PAGES, timezone: str = DEFAULT_TZ,
                     max_rows: int = 0, sleep: float = 0.3,
                     verbose: bool = False) -> List[Dict[str, Any]]:
    """`iter_orders` 的列表版（便于脚本直接拿全量）。"""
    return list(iter_orders(session, start, end, limit=limit, max_pages=max_pages,
                            timezone=timezone, max_rows=max_rows, sleep=sleep,
                            verbose=verbose))

# -*- coding: utf-8 -*-
"""
wb_ops WB 客服沟通（callcenter）投诉单适配器

封装 WB 卖家后台「投诉处理」子系统的只读接口：
- 投诉单列表  GET .../callcenter/v1/supplier/appeals（游标翻页，倒序）
- 投诉单详情  GET .../callcenter/v3/supplier/appeals/{appeal_id}（商品 nmId 仅此处下发）
鉴权与 WB 卖家后台同一套（authorizev3 + cookie），故直接复用 wb_client 的会话与重试封装。
"""
from typing import Any, Dict, List, Optional

from .. import common
from ..framework.exceptions import PlatformApiError
from . import wb_client as wb_api

APPEALS_LIST = (
    "https://seller-callcenter.wildberries.ru"
    "/ns/cro-communication-suppliers/callcenter/v1/supplier/appeals"
)
APPEALS_DETAIL = (
    "https://seller-callcenter.wildberries.ru"
    "/ns/cro-communication-suppliers/callcenter/v3/supplier/appeals/{appeal_id}"
)

PAGE_SIZE = 50      # 每页请求条数（抓包 UI 用 10；平台若封顶会自动多翻几页，总数由 total 兜底）
MAX_PAGES = 200     # 翻页安全上限（防死循环）


def fetch_appeals(
    session,
    appeal_type: str = "in",
    limit: int = 0,
    max_pages: int = MAX_PAGES,
    stats: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """游标翻页拉取投诉单列表（按 id 倒序，cursor = 本页最后一条 id）。

    Args:
        session: 已构建的 WB 店铺会话（wb_client.make_session）。
        appeal_type: 投诉方向，in=发往本店 / out=本店发出。
        limit: 最多返回条数，0=不限（翻页到底）。
        max_pages: 翻页硬上限，防平台游标异常导致死循环。
        stats: 可选出参字典，会写入 total（平台声明的总数）与 pages（实际翻页数）。

    Returns:
        投诉单列表（原始字典）。

    Raises:
        CookieExpiredError: cookie 失效（403），由 wb_client.request 抛出。
        PlatformApiError: 平台返回其它错误码。
    """
    items: List[Dict[str, Any]] = []
    cursor: Optional[int] = None
    total = 0
    pages = 0
    for _ in range(max_pages):
        params: Dict[str, Any] = {"limit": PAGE_SIZE, "type": appeal_type}
        if cursor is not None:
            params["cursor"] = cursor
        d = wb_api.request(session, "GET", APPEALS_LIST, params=params)
        pages += 1
        if not isinstance(d, dict):
            break
        data = d.get("data")
        if not isinstance(data, list) or not data:
            break
        total = common.to_int(d.get("total")) or total
        items.extend(data)
        if limit > 0 and len(items) >= limit:
            break
        if total:
            # 平台声明了总数 → 以 total 为终止依据（即使服务端对 limit 封顶也能拉全）
            if len(items) >= total:
                break
        elif len(data) < PAGE_SIZE:
            break
        last_id = common.to_int(data[-1].get("id"))
        if not last_id or last_id == cursor:
            break
        cursor = last_id
    if stats is not None:
        stats["total"] = total
        stats["pages"] = pages
    return items[:limit] if limit > 0 else items


def fetch_appeal_detail(session, appeal_id: Any) -> Optional[Dict[str, Any]]:
    """拉取单条投诉详情（含商品 nmId）；平台错误（404/5xx 等）降级返回 None，不中断整店。

    Args:
        session: 已构建的 WB 店铺会话。
        appeal_id: 投诉单 id。

    Returns:
        详情字典；平台错误时为 None。

    Raises:
        CookieExpiredError: cookie 失效（403）——属店铺级故障，由调用方中止该店。
    """
    url = APPEALS_DETAIL.format(appeal_id=appeal_id)
    try:
        return wb_api.request(session, "GET", url)
    except common.CookieExpiredError:
        raise
    except PlatformApiError:
        return None

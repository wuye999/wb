# -*- coding: utf-8 -*-
"""
wb_ops WB 商品评分与评价分析适配器 (wb_feedback_client)

封装 WB 卖家后台「内容分析 - 商品评分与评价」官方接口：
- POST https://seller-content.wildberries.ru/ns/analytics-api/content-analytics/api/v2/product-feedbacks/distribution/details
鉴权复用 wb_client 会话（authorizev3 + cookie + seller-lk）。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional
import requests

from .. import common
from ..framework.exceptions import PlatformApiError
from . import wb_client as wb_api

FEEDBACK_DETAILS_URL = (
    "https://seller-content.wildberries.ru"
    "/ns/analytics-api/content-analytics/api/v2/product-feedbacks/distribution/details"
)

PAGE_SIZE = 100
MAX_PAGES = 100


def default_period_dates(days: int = 365) -> tuple[str, str]:
    """根据天数生成起止日期字符串 (YYYY-MM-DD)"""
    today = datetime.now().date()
    start = today - timedelta(days=days)
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def fetch_feedback_page(
    session: requests.Session,
    start_date: str,
    end_date: str,
    order_by_field: str = "feedbackRating",
    order_by_mode: str = "desc",
    offset: int = 0,
    limit: int = PAGE_SIZE,
    include_shadowed: bool = False,
) -> List[Dict[str, Any]]:
    """单页拉取商品评分与评价详情列表。

    Args:
        session: 已初始化的 WB requests 会话。
        start_date: 开始日期 (YYYY-MM-DD)。
        end_date: 结束日期 (YYYY-MM-DD)。
        order_by_field: 排序字段 (feedbackRating, oneStar, feedbackCount 等)。
        order_by_mode: 排序方式 (desc / asc)。
        offset: 偏移量。
        limit: 每页条数 (建议 <= 100)。
        include_shadowed: 是否包含被屏蔽商品。

    Returns:
        商品评价详情字典列表。
    """
    payload = {
        "currentPeriod": {
            "start": start_date,
            "end": end_date,
        },
        "nmIds": [],
        "subjectIds": [],
        "brandNames": [],
        "tagIds": [],
        "isNotIncludeNMsWithoutSales": False,
        "orderBy": {
            "field": order_by_field,
            "mode": order_by_mode,
        },
        "includeShadowedNMs": include_shadowed,
        "includeFeedbacksForPointsBadge": False,
        "limit": limit,
        "offset": offset,
    }
    resp = wb_api.request_post(session, FEEDBACK_DETAILS_URL, payload)
    if not isinstance(resp, dict):
        return []
    data = resp.get("data") or {}
    return data.get("products") or []


def iter_feedback_products(
    session: requests.Session,
    start_date: str,
    end_date: str,
    order_by_field: str = "feedbackRating",
    order_by_mode: str = "desc",
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    limit: int = 0,
    stop_on_zero_one_star: bool = False,
    stop_on_zero_feedback: bool = False,
) -> Iterator[Dict[str, Any]]:
    """按页遍历商品评价数据，支持基于排序特征的提前截断。

    Args:
        session: WB requests 会话。
        start_date: 开始日期。
        end_date: 结束日期。
        order_by_field: 排序字段。
        order_by_mode: 排序方式。
        page_size: 每页拉取数量。
        max_pages: 最大翻页数上限。
        limit: 最多返回条数 (0 表示不限)。
        stop_on_zero_one_star: 当按 1 星倒序且遇到 1 星数为 0 时是否提前截断。
        stop_on_zero_feedback: 当按评价总数倒序且遇到评价数为 0 时是否提前截断。

    Yields:
        每个商品的评价原始字典。
    """
    count = 0
    offset = 0

    for _ in range(max_pages):
        products = fetch_feedback_page(
            session=session,
            start_date=start_date,
            end_date=end_date,
            order_by_field=order_by_field,
            order_by_mode=order_by_mode,
            offset=offset,
            limit=page_size,
        )
        if not products:
            break

        for prod in products:
            if stop_on_zero_one_star and order_by_field == "oneStar" and order_by_mode == "desc":
                one_s = common.to_int(prod.get("oneStar", {}).get("current", 0))
                if one_s <= 0:
                    return

            if stop_on_zero_feedback and order_by_field == "feedbackCount" and order_by_mode == "desc":
                cnt = common.to_int(prod.get("feedbackCount", {}).get("current", 0))
                if cnt <= 0:
                    return

            yield prod
            count += 1
            if limit > 0 and count >= limit:
                return

        if len(products) < page_size:
            break
        offset += page_size

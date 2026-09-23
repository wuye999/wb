# -*- coding: utf-8 -*-
"""
wb_ops Wildberries 广告推广（cmp.wildberries.ru）适配器

封装「推广活动列表 → 活动内被推广商品」的只读接口：

    GET /api/v1/adverts?page_number=..&page_size=..&status=[4,9,11]
        &order=createDate&direction=desc&autofill=all&bid_type=[1,2]&type=[8,9]&show_stocks=true

被推广商品内嵌在 `content[].stocks.products[]`（`show_stocks=true` 才下发），
字段：nm（WB 商品码）/ name（俄文标题）/ subject.name（类目）/ total_quantity_fbo|mp（活动内库存）。

抓包来源：api/网络请求/wb推广活动列表.har（2026-09-23，袁州3 实测）
鉴权：WB cookie 三件套会话（wb_client.make_session）+ cmp 域三个必需头
    authorization: Bearer <authorizev3>   （与 authorizev3 同值）
    authorizev3:  <shop.authorizev3>
    x-supplierid: <供应商 UUID，取店铺 cookie 的 x-supplier-id-external>

复用铁律（docs/REUSE_GUIDE.md 第 1 条）：不新建 HTTP 会话与重试策略，
一律走 wb_client.make_session + wb_client.request（403 → CookieExpiredError，[1,3,8] 退避）。
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import common
from . import wb_client as wb_api

CMP_BASE = "https://cmp.wildberries.ru"
URL_ADVERTS = CMP_BASE + "/api/v1/adverts"
URL_SUPPLIERS = CMP_BASE + "/api/v5/suppliers"

# 与后台默认视图一致（HAR 实测 status=[4,9,11]；9=在投 / 11=暂停 / 4=其它）
STATUS_DEFAULT: Tuple[int, ...] = (4, 9, 11)
BID_TYPE_Q = "%5B1,2%5D"      # [1,2]
TYPE_Q = "%5B8,9%5D"          # [8,9]

_SUPPLIER_COOKIE_RE = re.compile(r"x-supplier-id-external=([^;\s]+)")


def supplier_uuid_from_cookie(shop: Dict[str, Any]) -> str:
    """从店铺 cookie 串解析供应商 UUID（x-supplier-id-external）。

    实测 credentials.json 三店 cookie 均带该字段，且与 HAR 抓包的 x-supplierid 一致。
    """
    m = _SUPPLIER_COOKIE_RE.search(shop.get("cookie") or "")
    return m.group(1) if m else ""


def supplier_uuid(session, shop: Dict[str, Any]) -> str:
    """店铺 → 供应商 UUID：优先 cookie，取不到时兜底 GET /api/v5/suppliers（按 oldID==shopId 匹配）。"""
    uid = supplier_uuid_from_cookie(shop)
    if uid:
        return uid
    shop_id = common.to_int(shop.get("shopId") or shop.get("shop_id"))
    d = wb_api.request(session, "GET", URL_SUPPLIERS, headers=cmp_headers(shop, supplier_id=""))
    for s in ((d.get("result") or {}).get("suppliers") or []):
        if common.to_int(s.get("oldID")) == shop_id:
            return str(s.get("id") or "")
    return ""


def cmp_headers(shop: Dict[str, Any], supplier_id: Optional[str] = None) -> Dict[str, str]:
    """cmp.wildberries.ru 域必需请求头。

    make_session 默认的 Referer/Origin 指向 seller.wildberries.ru，本域逐请求覆盖；
    （requests 逐请求 headers 优先于 session.headers，故无需改 make_session）。
    supplier_id 显式传空串 → 不下发 x-supplierid（用于 UUID 兜底查询本身）。
    """
    uid = supplier_uuid_from_cookie(shop) if supplier_id is None else supplier_id
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorizev3": shop["authorizev3"],
        "authorization": "Bearer " + shop["authorizev3"],
        "lang": "ru",
        "x-client-trace": "promotion-campaigns",
        "referer": CMP_BASE + "/campaigns/list",
        "origin": CMP_BASE,
    }
    if uid:
        headers["x-supplierid"] = uid
    return headers


def adverts_url(page_number: int, page_size: int, statuses=STATUS_DEFAULT) -> str:
    """构造 adverts 查询串：`[`/`]` 按 HAR 百分号编码、逗号保留，与后台抓包逐字节一致。"""
    status_q = "%5B" + ",".join(str(common.to_int(s)) for s in statuses) + "%5D"
    return (f"{URL_ADVERTS}?page_number={common.to_int(page_number, 1)}"
            f"&page_size={common.to_int(page_size, 100)}"
            f"&status={status_q}&order=createDate&direction=desc&autofill=all"
            f"&bid_type={BID_TYPE_Q}&type={TYPE_Q}&show_stocks=true")


def fetch_adverts(
    session,
    shop: Dict[str, Any],
    statuses=STATUS_DEFAULT,
    page_size: int = 100,
    max_pages: int = 50,
    limit: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """分页拉取推广活动列表（含 stocks.products 被推广商品）。

    收敛条件（任一命中即停）：累计 >= counts.totalCount / 本页无数据 / 达 limit / 达 max_pages。
    返回 (campaigns, total_count)；total_count = -1 表示响应未给 counts（此时只靠空页收敛）。
    """
    headers = cmp_headers(shop)
    campaigns: List[Dict[str, Any]] = []
    seen_ids = set()
    total = -1
    for page in range(1, max(1, common.to_int(max_pages, 50)) + 1):
        d = wb_api.request(session, "GET", adverts_url(page, page_size, statuses), headers=headers)
        counts = d.get("counts") or {}
        if counts.get("totalCount") is not None:
            total = common.to_int(counts.get("totalCount"), -1)
        items = [it for it in (d.get("content") or []) if isinstance(it, dict)]
        if not items:
            break
        for it in items:
            cid = it.get("id")
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            campaigns.append(it)
        if limit and len(campaigns) >= limit:
            campaigns = campaigns[:limit]
            break
        if total >= 0 and len(campaigns) >= total:
            break
    return campaigns, total

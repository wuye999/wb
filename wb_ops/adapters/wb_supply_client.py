# -*- coding: utf-8 -*-
"""
wb_ops WB 原生卖家后台 FBS「配送中」适配器 (wb_supply_client)

接口（抓包来源：api/网络请求/wb订单列表配送中.har、wb订单列表配送中2.har）：
- 配送中清单列表: GET  marketplace.wildberries.ru/ns/marketplace-app/marketplace-remote-wh
                      /api/v3/portal/supplies/delivery?limit=<N>&next=<游标>&order=asc
                      → data.supplies[]（清单级）+ data.next（游标分页）
- 清单下商品明细: GET  .../api/v3/portal/supplies/{supplyID}/orders/delivery
                      ?next=<游标>&onlyTransferred=false&order=asc
                      → data.orders[]（商品级）+ data.next
                      ⚠ 该接口**不接受 limit**；返回该清单下**全部**订单（含已取消/已售出），
                        调用方必须按 state.code 自行过滤
- 订单计数(概览): GET  .../api/v3/portal/fbs/orders/count → data.{new,assemble,delivery,canceled}
                      ⚠ 口径是**订单数**，远大于清单数，禁止当清单总数用
- 仓库短列表:     GET  .../api/v3/portal/offices/stores/short?deliveryType=1 → data[]

鉴权：WB 三件套（wb_api.make_session），与 wb_stock_client.py 同域同会话，已实证可用。
会话由调用方构建，本模块不建 session。
分页统一按「游标 next」：data.next 为 falsy / 与上一页相同 / 超过 max_pages 时终止（防死循环）。
"""
from typing import Any, Dict, List, Optional

from wb_ops.adapters import wb_client as wb_api

BASE = ("https://marketplace.wildberries.ru/ns/marketplace-app/"
        "marketplace-remote-wh/api/v3/portal")
SUPPLIES_DELIVERY_URL = BASE + "/supplies/delivery"
SUPPLY_ORDERS_TPL = BASE + "/supplies/{sid}/orders/delivery"
ORDERS_COUNT_URL = BASE + "/fbs/orders/count"
STORES_SHORT_URL = BASE + "/offices/stores/short"

PAGE_SIZE = 100          # 抓包实测 limit=100
MAX_PAGES = 50           # 翻页安全上限（防死循环）


def iter_delivery_supplies(session, page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES,
                           order: str = "asc", limit: int = 0,
                           verbose: bool = False) -> List[Dict[str, Any]]:
    """分页拉取「配送中」清单 → list[dict]（清单级，含 supplyID/createdAt/state/ordersCnt 等）。

    Args:
        page_size: 每页条数（默认 100，对齐抓包）
        max_pages: 翻页安全上限
        order: asc/desc
        limit: >0 时取够 N 条即停（0=不限，翻到底）
        verbose: 打印每页进度
    """
    items: List[Dict[str, Any]] = []
    nxt: Any = 0
    page, seen = 0, None
    while True:
        params: Dict[str, Any] = {"limit": page_size, "order": order, "next": nxt}
        d = wb_api.request(session, "GET", SUPPLIES_DELIVERY_URL, params=params)
        data = d.get("data") or {}
        chunk = data.get("supplies") or []
        items.extend(chunk)
        page += 1
        if verbose:
            print(f"    [清单分页 {page}] 本页 {len(chunk)} 条，累计 {len(items)} 条", flush=True)
        if limit and len(items) >= limit:
            items = items[:limit]
            break
        nxt = data.get("next")
        if not nxt:
            break
        if str(nxt) == str(seen):
            break  # 游标不变 → 服务端异常，防死循环
        if page >= max_pages:
            print(f"    [警告] 清单列表超过 {max_pages} 页未结束，提前终止（数据异常）", flush=True)
            break
        seen = nxt
    return items


def iter_supply_orders(session, supply_id: str, order: str = "asc",
                       only_transferred: bool = False, max_pages: int = MAX_PAGES,
                       verbose: bool = False) -> List[Dict[str, Any]]:
    """分页拉取某清单下的商品/订单 → list[dict]（商品级）。

    注意：本接口**不接受 limit**，一次游标翻页到底；
    `onlyTransferred=false` 表示取全部（含已取消/已售出），由调用方按 state.code 过滤。
    """
    url = SUPPLY_ORDERS_TPL.format(sid=supply_id)
    ot = "true" if only_transferred else "false"
    items: List[Dict[str, Any]] = []
    nxt: Any = 0
    page, seen = 0, None
    while True:
        params: Dict[str, Any] = {"next": nxt, "onlyTransferred": ot, "order": order}
        d = wb_api.request(session, "GET", url, params=params)
        data = d.get("data") or {}
        chunk = data.get("orders") or []
        items.extend(chunk)
        page += 1
        if verbose:
            print(f"    [商品分页 {page}] 本页 {len(chunk)} 条，累计 {len(items)} 条", flush=True)
        nxt = data.get("next")
        if not nxt:
            break
        if str(nxt) == str(seen):
            break
        if page >= max_pages:
            print(f"    [警告] 清单 {supply_id} 商品列表超过 {max_pages} 页未结束，提前终止", flush=True)
            break
        seen = nxt
    return items


def fetch_orders_count(session) -> Dict[str, int]:
    """各 tab 订单计数 → {'new','assemble','delivery','canceled'}（口径=订单数，非清单数）。"""
    d = wb_api.request(session, "GET", ORDERS_COUNT_URL)
    data = d.get("data") or {}
    return {k: int(data.get(k) or 0) for k in ("new", "assemble", "delivery", "canceled")}


def fetch_stores_short(session, delivery_type: int = 1) -> Dict[int, Dict[str, Any]]:
    """仓库/门店短列表 → {id: {name, warehouseName, warehouseId}}（可选，中文化仓库名用）。"""
    d = wb_api.request(session, "GET", STORES_SHORT_URL, params={"deliveryType": delivery_type})
    rows = d.get("data") or []
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows if isinstance(rows, list) else []:
        rid = r.get("id")
        if rid is not None:
            out[rid] = r
    return out

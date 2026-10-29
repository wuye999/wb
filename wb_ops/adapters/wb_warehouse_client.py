# -*- coding: utf-8 -*-
"""wb_ops WB 原生「仓库/门店列表」适配器 (wb_warehouse_client)

接口（抓包来源：api/网络请求/wb获取仓库id.har，2026-10-29）：
    GET https://marketplace.wildberries.ru/ns/marketplace-app/marketplace-remote-wh
        /api/v3/portal/offices/stores/short?deliveryType=1&order=asc
    → {"data":[{"name","warehouseName","warehouseAddress","id","warehouseId",
                "cargoType","deliveryType","isProcessing","isDeleting"}, ...],
       "error":false,"errorText":"","additionalErrors":{"errorCode":""}}

⚠⚠ 字段陷阱（用错会清到错误仓库，且不会报错）⚠⚠
    本响应里 **`id` = 仓库/门店 ID（如 1929635）** —— 这就是项目一直用的 warehouseId；
    而叫 **`warehouseId` 的字段是 officeId（211 / 3006477）**，是另一套编号，**不要用**。
    验证链：offices/stores/short.id == portal/stocks.storeId == POST portal/stocks/{id}
             == BCS wbWarehouses/list[].id == 旧的 known_warehouses.json 值。

鉴权：仅 WB 三件套（authorizev3 + seller-lk + root-version），**不需要 BCS**。
会话由本模块按 shop 构建（与 stock_wb.set_stock 同风格）。
"""
from typing import Any, Dict, List

from wb_ops.adapters import wb_client as wb_api

STORES_SHORT_URL = ("https://marketplace.wildberries.ru/ns/marketplace-app/"
                    "marketplace-remote-wh/api/v3/portal/offices/stores/short")


def get_stores(shop: Dict[str, Any], *, delivery_type: int = 1,
               root_version: str = None) -> List[Dict[str, Any]]:
    """拉取本账号仓库/门店短列表 → list[dict]（原始行，已剔除 isDeleting）。

    Args:
        shop: credentials.wb_shop(sid) 的店铺 dict（含三件套）
        delivery_type: 1=FBS 直发（本项目清库存/下架/上架均用该模式）
        root_version: 不传则用 credentials 里的全局值

    Returns:
        原始行列表，每行关键是 **id**（仓库 ID）与 name（中文名，如「莫斯科仓库」）。
        isDeleting=True 的条目被过滤；isProcessing 保留（由调用方判断）。

    异常：CookieExpiredError 透传（由 make_session/request 抛出），其余异常原样抛。
    """
    session = wb_api.make_session(shop, root_version)
    d = wb_api.request(session, "GET", STORES_SHORT_URL,
                       params={"deliveryType": int(delivery_type), "order": "asc"})
    if d.get("error"):
        raise RuntimeError(f"仓库列表接口返回 error: {d.get('errorText') or d.get('error')}")
    rows = d.get("data")
    if not isinstance(rows, list):
        return []
    return [r for r in rows
            if isinstance(r, dict) and r.get("id") is not None and not r.get("isDeleting")]

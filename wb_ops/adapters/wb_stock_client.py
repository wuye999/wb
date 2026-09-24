# -*- coding: utf-8 -*-
"""
wb_ops WB 原生卖家后台「库存」适配器 (wb_stock_client)

接口（抓包来源：api/网络请求/wb在线加载修改库存.har、wb在线根据供应商代码查询商品库存.har）：
- 查询: GET  marketplace.wildberries.ru/ns/marketplace-app/marketplace-remote-wh/api/v3/portal/stocks
         ?order=asc&next=<游标>[&search=<vendorCode>][&stores=<storeId>]
         → data.next 游标分页 + data.stocks[]（article=vendorCode / chrtId / nmId / amount / storeId）
- 修改: POST .../api/v3/portal/stocks/{warehouseId}
         body {"data":[{"chrtId":X,"amount":N}, ...]}（数组天然批量，官方同构端点单请求支持 ≤1000 SKU）
         → 同步返回 {"error":false,"errorText":"","data":{}}

鉴权：WB 三件套（wb_api.make_session），与 remote_wh.py 同域同会话，已实证可用。
会话由调用方构建，本模块不建 session。
"""
from typing import Dict, Any, List, Optional

from wb_ops.adapters import wb_client as wb_api

STOCKS_V3_URL = ("https://marketplace.wildberries.ru/ns/marketplace-app/"
                 "marketplace-remote-wh/api/v3/portal/stocks")

# 修改接口单批上限：官方同构端点 /api/v3/stocks/{whId} 为 1000/请求；
# portal 端点数组容量经 _scratch 探针实测（见 ops_result/工作日志），默认保守 200。
MAX_CHUNK = 1000


def get_stocks(session, store_id: Optional[int] = None, search: str = "",
               max_pages: int = 200, verbose: bool = False) -> List[Dict[str, Any]]:
    """分页拉取库存条目 → list[dict]（含 article/chrtId/nmId/storeId/amount/name）。

    游标分页：data.next 非空则继续；超过 max_pages 强制终止防死循环。
    store_id 传 None 时不带 stores 参数（账号级）；search 传 vendorCode 精确匹配（HAR3 实证）。
    """
    items: List[Dict[str, Any]] = []
    nxt, page, seen = "", 0, None
    while True:
        params: Dict[str, Any] = {"order": "asc"}
        if store_id is not None:
            params["stores"] = store_id
        if search:
            params["search"] = search
        if nxt:
            params["next"] = nxt
        d = wb_api.request(session, "GET", STOCKS_V3_URL, params=params)
        data = d.get("data") or {}
        chunk = data.get("stocks") or []
        items.extend(chunk)
        page += 1
        nxt = data.get("next")
        if verbose:
            print(f"    [分页 {page}] 本页 {len(chunk)} 条，累计 {len(items)} 条", flush=True)
        if not nxt:
            break
        if str(nxt) == str(seen):
            break  # 游标不变 → 服务端异常，防死循环
        if page >= max_pages:
            print(f"    [警告] 超过 {max_pages} 页未结束，提前终止（数据异常）", flush=True)
            break
        seen = nxt
    return items


def post_stocks(session, warehouse_id: int, chrt_items: List[Dict[str, int]],
                allow_400_json: bool = True) -> Dict[str, Any]:
    """提交批量库存修改（POST v3 portal/stocks/{warehouseId}）。

    Args:
        chrt_items: [{"chrtId": X, "amount": N}, ...]（调用方负责分块，单批建议 ≤MAX_CHUNK）
        allow_400_json: WB 部分接口 400 也带 JSON 错误体（{"error":true,"errorText":...}），按包裹返回

    Returns:
        原始响应包裹 {"error": bool, "errorText": str, "data": ...}，由 service 层判 ok。
    """
    body = {"data": [{"chrtId": int(it["chrtId"]), "amount": int(it["amount"])}
                     for it in chrt_items]}
    return wb_api.request_post(session, f"{STOCKS_V3_URL}/{int(warehouse_id)}",
                               body, allow_400_json=allow_400_json)

"""orders。"""
import json
from datetime import datetime, timedelta, timezone

import time
import urllib.parse
import requests


from sellerops.integrations.mabang.client import AAMZ_BASE
from sellerops.integrations.mabang.client import REQUEST_INTERVAL
from sellerops.integrations.mabang.client import WWW_BASE
from sellerops.integrations.mabang.client import aamz_headers
from sellerops.integrations.mabang.client import api_post
from sellerops.integrations.mabang.client import parse_json
from sellerops.integrations.mabang.client import www_headers

def fetch_pending_orders(cred: dict, days: int = 0, page_size: int = 100,
                         max_pages: int = 200) -> list[dict]:
    """查询新增待处理订单并完整翻页；days=0 不发送日期限制。

    正数 days 显式限制最近 N 天，日期按马帮接口 UTC+8 生成。
    达到分页上限或分页不前进时报错，不能把部分结果当作全部订单。
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days 必须为非负整数，0 表示不限日期")
    if page_size <= 0 or max_pages <= 0:
        raise ValueError("page_size 和 max_pages 必须大于0")
    start = end = ""
    if days:
        now = datetime.now(timezone(timedelta(hours=8)))
        end = now.strftime("%Y-%m-%d %H:%M:%S")
        start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    base_form = {
        "OrderPlus.isNewOrder": "1", "isSyncVal": "", "isSyncValIsVirtual": "",
        "isSyncLogisticsOrder": "", "isPackOrder": "", "isDeliverOrder": "",
        "isWaitPickupOrder": "", "isPendingOrder": "", "isOutOfStockOrder": "",
        "outOfStockOrderDay": "", "isSyncLogistics": "", "logisStatus": "",
        "isExpireOrder": "", "isWindControlOrder": "", "isShipmentOrderC": "",
        "isToDayOrder": "", "isToDayDeliveryOrder": "", "isResendOrderC": "",
        "isLogisticsRuleNotMatch": "", "noTrackOnlineDay": "", "quickPickType": "",
        "smtflag": "", "fbaFlag": "", "platformIdFbw": "", "shopeeAbnormal": "",
        "abnormalType": "", "cloudStatus": "", "isTuotou": "", "platformId": "",
        "leftSearchToWms": "", "getCompanyCloudStorageHtmlForJson": "[]",
        "supplierCompanyId_v": "", "orderBys[]": "", "postDta": "",
        "isshowordercombosku": "1", "title_Json": "", "platformTracknumberSearchInput": "",
        "platformTracknumberSearchtextarea": "", "orderSearchHistory": "",
        "OrderLogisticsSearch": "", "failureYiSearch": "", "view-hidden": "",
        "statusButton": "", "Order.orderStatus": "99", "orderTypeButton": "",
        "labelMultipleChoiceWhere": "cross", "byField": "1",
        "startTime1": start, "endTime1": end,
        "OrderPlus.isTrackOnline": "", "OrderSearch.fuzzySearchKey": "Order.platformOrderId",
        "OrderSearchFuSKey": "a.platformOrderId", "daysOperator": "=",
        "OrderSearch.fuzzySearchValue": "", "startPageNum": "", "endPageNum": "",
        "goPaypalRefundStatus": "1", "page": "1", "rowsPerPage": str(page_size),
        "Order_isCloud": "2", "m": "order", "a": "list", "isNewOrderPage": "1",
        "post_tableBase": "1", "showError": "", "pageListC": "",
        "jumpParams": "undefined", "1": "1", "tabId": "7",
        "global_company_config_json_str": "",
    }
    orders, page = [], 1
    seen_ids = set()
    while page <= max_pages:
        form = dict(base_form)
        form["page"] = str(page)
        r = requests.post(WWW_BASE, params={"mod": "order.oTc"}, headers=www_headers(cred),
                          data=urllib.parse.urlencode(form), timeout=60)
        r.raise_for_status()
        d = parse_json(r)
        if not d.get("success"):
            raise RuntimeError(f"order.oTc 返回失败: {d.get('message')}")
        batch = d.get("orderDataList")
        if not isinstance(batch, list):
            raise RuntimeError("order.oTc 缺少有效订单列表，不能当作无待处理订单")
        ids = [str(o.get("id") or "") for o in batch if isinstance(o, dict)]
        if len(ids) != len(batch) or any(not oid for oid in ids):
            raise RuntimeError("order.oTc 返回无效订单行或缺少订单ID")
        if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
            raise RuntimeError("order.oTc 订单分页重复，已中止以避免重复处理")
        seen_ids.update(ids)
        orders.extend(batch)
        print(f"  第 {page} 页: +{len(batch)} 单（累计 {len(orders)}）")
        if len(batch) < page_size:
            return orders
        if page == max_pages:
            raise RuntimeError(f"待处理订单超出分页上限 {max_pages}，结果不完整，未放行处理")
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return orders

def fetch_all_orders(cred, page_size=500, max_pages=60):
    """查询全部状态订单（order.oTc 全量变体：isNewOrder=2 / a=orderalllist / tabId=222）。"""
    orders, page = [], 1
    while page <= max_pages:
        form = {
            "OrderPlus.isNewOrder": "2", "isSyncVal": "", "isSyncValIsVirtual": "",
            "isSyncLogisticsOrder": "", "isPackOrder": "", "isDeliverOrder": "",
            "isWaitPickupOrder": "", "isPendingOrder": "", "isOutOfStockOrder": "",
            "outOfStockOrderDay": "", "isSyncLogistics": "", "logisStatus": "",
            "isExpireOrder": "", "isWindControlOrder": "", "isShipmentOrderC": "",
            "isToDayOrder": "", "isToDayDeliveryOrder": "", "isResendOrderC": "",
            "isLogisticsRuleNotMatch": "", "noTrackOnlineDay": "", "quickPickType": "",
            "smtflag": "", "fbaFlag": "", "platformIdFbw": "", "shopeeAbnormal": "",
            "abnormalType": "", "cloudStatus": "", "isTuotou": "", "platformId": "",
            "leftSearchToWms": "", "getCompanyCloudStorageHtmlForJson": "[]",
            "supplierCompanyId_v": "", "orderBys[]": "", "postDta": "",
            "isshowordercombosku": "1", "title_Json": "", "platformTracknumberSearchInput": "",
            "platformTracknumberSearchtextarea": "", "orderSearchHistory": "",
            "OrderLogisticsSearch": "", "failureYiSearch": "", "view-hidden": "",
            "statusButton": "", "orderTypeButton": "", "labelMultipleChoiceWhere": "cross",
            "byField": "1", "startTime1": "", "endTime1": "",
            "orderCursorMode": "1",
            "OrderPlus.isTrackOnline": "", "OrderSearch.fuzzySearchKey": "Order.platformOrderId",
            "OrderSearchFuSKey": "a.platformOrderId", "daysOperator": "=",
            "OrderSearch.fuzzySearchValue": "", "startPageNum": "", "endPageNum": "",
            "goPaypalRefundStatus": "1", "page": str(page), "rowsPerPage": str(page_size),
            "Order_isCloud": "2", "m": "order", "a": "orderalllist", "isNewOrderPage": "1",
            "post_tableBase": "1", "showError": "", "pageListC": "",
            "jumpParams": "undefined", "1": "1", "tabId": "222",
            "global_company_config_json_str": "",
        }
        r = requests.post(WWW_BASE, params={"mod": "order.oTc"}, headers=www_headers(cred),
                          data=urllib.parse.urlencode(form), timeout=120)
        r.raise_for_status()
        d = parse_json(r)
        if not d.get("success"):
            raise RuntimeError(f"order.oTc(orderalllist) 返回失败: {d.get('message')}")
        batch = d.get("orderDataList") or []
        orders.extend(batch)
        print(f"  第 {page} 页: +{len(batch)} 单（累计 {len(orders)}，总单数 {d.get('pageCount')}）")
        if len(batch) < page_size:
            break
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return orders

def fetch_order_item_ids(cred, order_id):
    """order.showOrderItems → 该订单的 orderItemId 列表（stock_data 键）"""
    form = {"orderItemIq": order_id, "tableBase": "1", "source": "common",
            "cloudStatus": "", "orderItemBy": "platformSku asc,isCombo asc,id asc,stockId asc",
            "tabId": "7"}
    r = requests.post(WWW_BASE, params={"mod": "order.showOrderItems"},
                      headers=www_headers(cred), data=form, timeout=60)
    r.raise_for_status()
    d = parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"showOrderItems 返回失败: {d.get('message')}")
    return list((d.get("stock_data") or {}).keys())

def search_stock(cred, sku, warehouse_id, page_size=20):
    """按库存 SKU 精确搜索，返回 stock 条目或 None"""
    body = {"skuType": 1, "operate": "like", "stockWarehouseId": str(warehouse_id),
            "pageSize": page_size, "page": 1, "warehouseIds": [], "searchValue": sku,
            "status": [1, 2, 3, 4, 5], "stockSkuOperate": "like",
            "stockNameCNOperate": "like", "stockNameENOperate": "like",
            "warehouseOperate": "like"}
    r = api_post(cred, "/product/Stock/searchStockList", body)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 200:
        raise RuntimeError(f"searchStockList 失败: {d.get('message')}")
    want = sku.strip().lower()
    for it in d.get("data") or []:
        if str(it.get("stockSku", "")).strip().lower() == want:
            return it
    return None

def fetch_stock_list(cred, timeout=120):
    """拉取马帮全部库存 SKU（aamz 域）→ [{stockSku,nameCN,stockQuantity,statusText,stockPicture}]

    抓包来源：`api/网络请求/马帮获取库存SKU的库存.har`（page/rowsPerPage 留空 = 一次返回全量）。
    """
    form = {"searchKey": "Stock_stockSku", "operate": "likeStart", "orderBys[]": "",
            "Stock_stockSku": "", "Stock_nameCN": "", "Stock_nameEN": "",
            "Stock_defaultRetailNameCn": "", "StockPlus_financial": "",
            "search-content": "库存SKU", "searchValue": "", "status": "3",
            "parentCategoryId": "", "categoryId": "", "third_category_id": "",
            "parentBrandId": "", "list-brandId": "", "labelId": "", "buyerId": "",
            "developerIdM": "", "dev_assistant": "", "artDesignerId": "", "salesId": "",
            "defaultStockWarehouseDetailId": "", "livenessType": "", "isNewType": "",
            "stock_type": "", "isMachining": "", "showstart": "1", "isCloud": "",
            "isGift": "", "exceptionDeclaration": "", "singleWarehouseType": "",
            "isGoogsExpireManageSearch": "", "page": "", "rowsPerPage": "",
            "stockOrderby": "a.stockQuantity desc"}
    r = requests.post(AAMZ_BASE, params={"mod": "stock.getStockList"},
                      headers=aamz_headers(cred), data=form, timeout=timeout)
    r.raise_for_status()
    d = json.loads(r.text.lstrip("\ufeff"))
    if not d.get("success"):
        raise RuntimeError(f"stock.getStockList 返回失败: {str(d)[:150]}")
    return [{"stockSku": it.get("stockSku") or "",
             "nameCN": it.get("nameCN") or "",
             "stockQuantity": it.get("stockQuantity"),
             "statusText": it.get("statusText") or "",
             "stockPicture": it.get("stockPicture") or ""}
            for it in (d.get("stockData") or [])]

def download_file(url, out_path, timeout=30):
    """下载远端文件到本地路径（图片附件上传用）。成功返回 True，失败返回 False（不抛异常）。"""
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception:
        return False

def replace_order_item(cred, order_item_id, stock_id, warehouse_id):
    """更换订单商品（replaceOrderItem）"""
    body = {"orderItemId": str(order_item_id), "stockId": int(stock_id),
            "skuType": 1, "warehouseId": int(warehouse_id)}
    r = api_post(cred, "/order/order/replaceOrderItem", body)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 200:
        raise RuntimeError(f"replaceOrderItem 失败: {d.get('msg') or d.get('message')}")
    return d

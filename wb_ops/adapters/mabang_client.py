# -*- coding: utf-8 -*-
"""
wb_ops 马帮 ERP 客户端适配器 (mabang_client)
封装与马帮 ERP 各个子域（www、aamz、api v2、sso）的网络交互、会话凭据管理、Token 自动换发与异常处理。
"""
import json
import re
import time
import urllib.parse
import requests
from wb_ops import common

WWW_BASE = "https://www.mabangerp.com/index.php"
AAMZ_BASE = "https://aamz.mabangerp.com/index.php"
API_BASE = "https://api.mabangerp.com/v2"
SSO_GET_TOKEN_URL = "https://api.mabangerp.com/sso/api/v1/getTokenByKey"

REQUEST_INTERVAL = 0.6          # 订单明细/更换请求间隔秒
PLATFORM_ID_WB = ""             # 空=全部平台（列表里按 platformIdText 过滤 Wildberries）
DEF_FORECAST_LOGISTICS = "2369||262534"   # Wildberries线上发货（getForecastLogistics 默认）
DEF_FORECAST_CHANNEL = "wb_box"           # wildberries组包服务+打印箱贴(MP)

CHANNEL_OBJ_RE = re.compile(
    r'\{"id":"(\d+)","source":"\d+","logisticsId":"(\d+)","myLogisticsId":"(\d+)",'
    r'"logisticsChannelName":"((?:\\u[0-9a-fA-F]{4})+)"')


def get_mabang_cred():
    """读 credentials.json 的 mabang 段。"""
    from wb_ops import credentials
    data = credentials.get().data.get("mabang") or {}
    missing = [k for k in ("www_cookie", "shop_map") if not data.get(k)]
    if missing:
        raise RuntimeError(f"credentials.json 缺少 mabang.{missing[0]}（马帮凭证/店铺映射不完整）")
    return data


def cookie_value(cookie_str, name):
    """从 Cookie 字符串提取指定字段值"""
    m = re.search(re.escape(name) + r"=([^;]+)", cookie_str or "")
    return m.group(1) if m else ""


def api_key_from_www(cred):
    return cookie_value(cred.get("www_cookie", ""), "MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE")


def api_ready(cred):
    """api 域凭证是否可用：有 Bearer，或 www_cookie 里能提取 key（可自动续期）"""
    return bool(cred.get("api_bearer")) or bool(api_key_from_www(cred))


def refresh_api_token(cred):
    """用 key（www cookie 提取）调 sso getTokenByKey 换发新 Bearer 并写回 credentials.json；返回新 token。"""
    key = api_key_from_www(cred)
    if not key:
        raise RuntimeError("www_cookie 中未找到 MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE，无法自动续期 api token")
    r = requests.post(
        SSO_GET_TOKEN_URL,
        headers={"User-Agent": common.UA, "Content-Type": "application/json",
                 "ProjectId": "erp", "cluster-id": "1", "lang": "cn",
                 "TimeZone": "UTC+8"},
        data=json.dumps({"key": key, "lang": "zh"}),
        timeout=30,
    )
    d = parse_json(r)
    token = (d.get("data") or {}).get("token")
    if d.get("code") != 200 or not token:
        raise RuntimeError(f"getTokenByKey 续期失败: {str(d)[:150]}")
    from wb_ops import credentials
    c = credentials.get()
    mb = c.data.setdefault("mabang", {})
    mb["api_bearer"] = token
    mb["api_key"] = key
    with open(c.path, "w", encoding="utf-8") as f:
        json.dump(c.data, f, ensure_ascii=False, indent=2)
    c.reload()
    return token


def www_headers(cred):
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.mabangerp.com",
        "Referer": "https://www.mabangerp.com/index.php?mod=order.list",
        "Cookie": cred["www_cookie"],
    }


def api_headers(cred):
    bearer = cred.get("api_bearer") or ""
    if not bearer:
        bearer = refresh_api_token(cred)
    key = cred.get("api_key") or api_key_from_www(cred)
    if not key:
        raise RuntimeError("无法确定 api 域 key 头（www_cookie 中无 MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE）")
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/json",
        "Authorization": "Bearer " + bearer,
        "key": key,
        "ProjectId": "erp",
        "TimeZone": "UTC+8",
        "cluster-id": "1",
        "lang": "cn",
    }


def aamz_headers(cred):
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://aamz.mabangerp.com",
        "Referer": "https://aamz.mabangerp.com/index.php?mod=uploadforecastorderv2",
        "Cookie": cred.get("aamz_cookie") or cred["www_cookie"],
    }


def parse_json(resp):
    return resp.json()


def api_post(cred, path, body):
    """api 域 POST：401 时自动续期 Bearer 后重试一次"""
    payload = json.dumps(body)
    for attempt in (1, 2):
        r = requests.post(API_BASE + path, headers=api_headers(cred), data=payload, timeout=60)
        if r.status_code != 401:
            return r
        if attempt == 2:
            break
        print("  [api] 401，自动续期 Bearer 后重试...")
        refresh_api_token(cred)
        cred = get_mabang_cred()
    return r


# ---------------- 订单列表与明细查询 ----------------
def fetch_pending_orders(cred, days=30, page_size=100, max_pages=20):
    """查询待处理订单列表（order.oTc），自动翻页，返回原始订单 dict 列表"""
    end = time.strftime("%Y-%m-%d %H:%M:%S")
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
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
    while page <= max_pages:
        form = dict(base_form)
        form["page"] = str(page)
        r = requests.post(WWW_BASE, params={"mod": "order.oTc"}, headers=www_headers(cred),
                          data=urllib.parse.urlencode(form), timeout=60)
        r.raise_for_status()
        d = parse_json(r)
        if not d.get("success"):
            raise RuntimeError(f"order.oTc 返回失败: {d.get('message')}")
        batch = d.get("orderDataList") or []
        orders.extend(batch)
        print(f"  第 {page} 页: +{len(batch)} 单（累计 {len(orders)}）")
        if len(batch) < page_size:
            break
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


# ---------------- 预报批次 ----------------
def get_forecast_logistics(cred):
    """获取物流方式与默认预报参数"""
    r = requests.post(WWW_BASE, params={"mod": "ordera.getForecastLogistics"},
                      headers=www_headers(cred), data="", timeout=60)
    r.raise_for_status()
    d = parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"getForecastLogistics 返回失败: {d.get('message')}")
    s = d.get("createForecastSet") or {}
    logistics = s.get("platformForecastLogistics") or s.get("forecastLogistics") or DEF_FORECAST_LOGISTICS
    channel = s.get("platformForecastLogisticsChannel") or s.get("forecastLogisticsChannel") or DEF_FORECAST_CHANNEL
    return logistics, channel


def batch_create_forecast(cred, order_ids, logistics, channel):
    """生成预报批次，服务端按店铺自动拆批；返回 (批次号列表, 消息)"""
    form = {
        "tablebase": "1", "orderIds": ",".join(str(x) for x in order_ids),
        "forecastLogistics": logistics, "forecastLogisticsChannel": channel,
        "create_forecast_set": "2", "smtCrossStorePackVal": "1",
        "tiktokPackType": "tiktok_pickup", "tiktokCrossStorePack": "1",
        "wildberriesCrossPack": "1", "activeTab": "platform",
    }
    r = requests.post(WWW_BASE, params={"mod": "ordera.doBatchCreateForecast"},
                      headers=www_headers(cred), data=urllib.parse.urlencode(form), timeout=120)
    r.raise_for_status()
    d = parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"doBatchCreateForecast 返回失败: {d.get('message')}")
    batch_nos = [x for x in (d.get("batchNoStr") or d.get("batchNo") or "").split(",") if x]
    return batch_nos, d.get("message") or ""


def get_forecast_list(cred, status=1, rows_per_page=50):
    """待上传/历史预报批次列表（aamz 域）；返回 (orderList, 统计dict)"""
    form = {"searchType": "1", "createoperType": "", "printstatus": "", "cancelStatus": "",
            "numberId": "", "datepicker-from": "", "datepicker-to": "", "status": str(status),
            "uploadlogisticschannel": "", "type": "1", "page": "1",
            "rowsPerPage": str(rows_per_page)}
    r = requests.post(AAMZ_BASE, params={"mod": "uploadforecastorderv2.getForecastOrderList"},
                      headers=aamz_headers(cred), data=form, timeout=60)
    r.raise_for_status()
    d = parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"getForecastOrderList 返回失败: {d.get('message')}")
    stats = {k: d.get(k) for k in ("waitTotal", "succesTotal", "failTotal", "historyTotal")}
    return d.get("orderList") or [], stats


def get_forecast_config(cred, my_logistics_id="262534"):
    """取上传预报单配置模板（aamz 域）"""
    r = requests.post(AAMZ_BASE, params={"mod": "uploadforecastorderv2.getForecastConfig"},
                      headers=aamz_headers(cred), data={"myLogisticsId": my_logistics_id}, timeout=60)
    r.raise_for_status()
    d = parse_json(r)
    return d.get("forecast_config_json") if d.get("success") else None


def upload_forecast_batch(cred, batch_nos):
    """依次上传预报批次（aamz 域，异步队列，勾选自动发货 wb_automark=1）"""
    msgs = []
    for i, batch in enumerate(batch_nos):
        form = get_forecast_config(cred) or {}
        automark = bool(form)
        if form:
            form["batchNoInfo"] = batch
            form["wb_automark"] = "1"
            form["is_set_forecast"] = "2"
        else:
            form = {"batchNoInfo": batch, "forecastLogistics": "100",
                    "forecastChannel": "", "shipmentMethod": "", "appointment_type": "",
                    "expressCompany": "", "trackNumber": "", "shipmentDate": "",
                    "cainiaoShipmentDate": "", "shopeeV2PaymentMethod": "1",
                    "shippingAddressId": "", "lazadaReturnType": "",
                    "cainiaoRegularSchedulesDayesArr": "", "cainiaoStart": "", "cainiaoEnd": "",
                    "cainiaoPalletQuantity": "", "returnAddressId": "",
                    "smt_sfc_shipping_method": "3", "smt_sfc_car_type": "", "smt_sfc_goods_type": "",
                    "smt_sfc_box_number": "", "smt_sfc_weight": "", "smt_sfc_volume": "",
                    "smt_jit_shipping_method": "1", "smt_jit_shipping_service": "",
                    "smt_jit_shipping_tracknumber": "", "smt_jit_shipping_remark": "",
                    "smt_jit_car_number": "", "smt_jit_driver_phone": "",
                    "smt_jit_shipping_volume": "", "smt_shipping_except_method": "",
                    "smt_sfc_city_code": "", "smt_sfc_amount": "",
                    "lazada_fcs_shipping_method": "parcel", "lazada_fcs_shipping_service": "",
                    "lazada_fcs_shipping_tracknumber": "", "lazada_fcs_car_number": "",
                    "lazada_fcs_driver_phone": "", "lazada_fcs_pickup_date": "",
                    "lazada_fcs_shipping_weight": "", "lazada_fcs_shipping_volume": "",
                    "lazada_fcs_shipping_pack_num": "", "lazada_fcs_shipping_pickup_address": "",
                    "lazada_shop_type": "", "lazada_division_id": "", "joomBoxesCount": "",
                    "joomBoxesTotalWeight": "", "shein_fcs_shipping_method": "1",
                    "shein_fcs_reach_time": "", "shein_fcs_shipping_tracknumber": "",
                    "shein_fcs_pickup_time": "", "shein_fcs_package_number": "",
                    "shein_fcs_package_weight": "", "wb_supplier_no": ""}
        r = requests.post(AAMZ_BASE, params={"mod": "uploadforecastorderv2.uploadForecastBatch"},
                          headers=aamz_headers(cred), data=form, timeout=120)
        r.raise_for_status()
        d = parse_json(r)
        if not d.get("success"):
            raise RuntimeError(f"uploadForecastBatch({batch}) 返回失败: {d.get('message')}")
        msg = d.get("message") or ""
        tag = "自动发货" if automark else "普通"
        print(f"    [{i + 1}/{len(batch_nos)}] {batch} 上传成功（{tag}）")
        msgs.append(msg)
        if i + 1 < len(batch_nos):
            time.sleep(1)
    return msgs[-1] if msgs else ""


# ---------------- 物流交运 ----------------
def discover_handover_channel(cred):
    """从 order.list 页面内嵌渠道数组动态发现交运渠道（按 handover_keyword 匹配）"""
    keyword = cred.get("handover_keyword") or "七库海外仓"
    kw_esc = keyword.encode("unicode_escape").decode("latin-1")
    r = requests.get(WWW_BASE, params={"mod": "order.list", "Order_orderStatus": "2"},
                     headers={**www_headers(cred), "Content-Type": ""}, timeout=120)
    r.raise_for_status()
    txt = r.text
    for m in CHANNEL_OBJ_RE.finditer(txt):
        raw_name = m.group(4)
        if kw_esc not in raw_name and keyword not in raw_name:
            continue
        name = raw_name.encode("latin-1").decode("unicode_escape")
        cid, lid, mid = m.group(1), m.group(2), m.group(3)
        return f"{cid}_{mid}_{name}_{lid}"
    return None


def get_handover_channel_value(cred, sample_order_id):
    """解析交运渠道完整值"""
    keyword = cred.get("handover_keyword") or "七库海外仓"
    v = discover_handover_channel(cred)
    if v:
        return v
    print("  [提示] order.list 动态发现未命中，转配置渠道 id 接口解析...")
    channel_id = cred.get("handover_channel_id") or ""
    if channel_id:
        try:
            form = {"orderId": str(sample_order_id), "tableBase": "1",
                    "myLogisticsChannelId": channel_id, "orderLogisticsSearchId": ""}
            r = requests.post(WWW_BASE, params={"mod": "order.getReportingInformation"},
                              headers=www_headers(cred), data=form, timeout=60)
            r.raise_for_status()
            d = parse_json(r)
            if d.get("success"):
                for v in re.findall(r"val\('([^']*)'\)",
                                    d.get("reportingInformationMyLogisticsChannelHtml") or ""):
                    if keyword in v:
                        return v
            print("  [提示] getReportingInformation 未解析到渠道选项，转配置兜底...")
        except Exception as e:
            print(f"  [提示] getReportingInformation 失败({str(e)[:60]})，转配置兜底...")
    fallback = cred.get("handover_channel_value") or ""
    if fallback:
        print(f"  [警告] 动态发现失败，回退配置值 {fallback}")
        return fallback
    raise RuntimeError(f"未能确定交运渠道值（渠道数组/渠道 id/配置均未命中「{keyword}」）")


def set_handover(cred, order_ids, channel_value):
    """提交物流交运（doReportingInformation）；返回 (成功订单id集合, 未找到列表)"""
    form = {"myLogisticsChannelId": channel_value,
            "orderId": ",".join(str(x) for x in order_ids),
            "source": "3", "trackNumber": "", "tableBase": "1",
            "quickaccessBol": "2", "dataChanges": "1", "isSyncLogistics": "1"}
    r = requests.post(WWW_BASE, params={"mod": "order.doReportingInformation"},
                      headers=www_headers(cred), data=urllib.parse.urlencode(form), timeout=120)
    r.raise_for_status()
    d = parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"doReportingInformation 返回失败: {d.get('message')}")
    ok_ids = {str(x) for x in (d.get("successMessage") or [])}
    not_found = [str(x) for x in (d.get("notFoundPlatformOrderIds") or [])]
    return ok_ids, not_found

# -*- coding: utf-8 -*-
"""
wb_ops 马帮 ERP 待处理订单 SKU 匹配更换

流程：
  1. 查询马帮待处理订单列表（order.oTc，Wildberries 平台，含平台 SKU=vendorCode 与系统已匹配库存 SKU）
  2. 逐单从平台 SKU 提取 VC 码（BCS-{前缀}-{nmId}）→ 价格映射表「映射总表」查中文名
     → 商品价格表「库存SKU」列得到正确库存 SKU
  3. 与系统已匹配 SKU 比对（忽略大小写，仅作备注展示）：
       REPLACE 凡 VC 链路解析成功的订单一律标记待更换（★ 用户规则：无论系统是否已匹配、
               匹配是否一致，都强制更换为 VC 链路查到的 SKU，因系统匹配可能有错）
       NO_VC   VC 不在映射表（含 ozon-card 尾段等），仅标注
       NO_SKU  中文名在商品价格表无库存SKU，仅标注
       NO_BCS  平台 SKU 非 BCS- 开头，仅标注
  4. 更换：order.showOrderItems 取 orderItemId → /v2/product/Stock/searchStockList
     按正确 SKU 查 stockId → /v2/order/order/replaceOrderItem 提交

默认 dry-run（只出报表），--apply 才真正更换。结果 CSV 写 data/logs/。
"""
import csv
import json
import os
import re
import time
import urllib.parse

import requests

from . import config
from . import common

WWW_BASE = "https://www.mabangerp.com/index.php"
AAMZ_BASE = "https://aamz.mabangerp.com/index.php"
API_BASE = "https://api.mabangerp.com/v2"
VC_RE = re.compile(r"(BCS-[A-Z]{4}-(?:ozon-card-)?[A-Za-z0-9-]+?)\*?\d*$")
SKU_RE = re.compile(r"商品编号\*数量:(.+?)\*?\d*$")
TITLE_SKU_RE = re.compile(r"数量:(.*)$")
REQUEST_INTERVAL = 0.6          # 订单明细/更换请求间隔秒
PLATFORM_ID_WB = ""             # 空=全部平台（列表里按 platformIdText 过滤 Wildberries）


# ---------------- 凭证 ----------------
def _mabang_cred():
    """读 credentials.json 的 mabang 段。
    必填：www_cookie / shop_map；可选：api_bearer（SKU 搜索/更换需要，缺失时仅限
    非更换功能）、api_key（可从 www_cookie 的 MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE
    自动提取）、aamz_cookie（可省，自动回退 www_cookie）。"""
    from . import credentials
    data = credentials.get().data.get("mabang") or {}
    missing = [k for k in ("www_cookie", "shop_map") if not data.get(k)]
    if missing:
        raise RuntimeError(f"credentials.json 缺少 mabang.{missing[0]}（马帮凭证/店铺映射不完整）")
    return data


def _api_ready(cred):
    """api 域凭证是否可用：有 Bearer，或 www_cookie 里能提取 key（可自动续期）"""
    return bool(cred.get("api_bearer")) or bool(_api_key_from_www(cred))


def _api_key_from_www(cred):
    return _cookie_value(cred.get("www_cookie", ""),
                         "MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE")


SSO_GET_TOKEN_URL = "https://api.mabangerp.com/sso/api/v1/getTokenByKey"


def refresh_api_token(cred):
    """用 key（www cookie 提取）调 sso getTokenByKey 换发新 Bearer 并写回
    credentials.json 的 mabang.api_bearer/api_key；返回新 token。
    2026-09-08 实测：仅 body.key 即可换发（无需旧 Bearer）。"""
    key = _api_key_from_www(cred)
    if not key:
        raise RuntimeError("www_cookie 中未找到 MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE，"
                           "无法自动续期 api token")
    r = requests.post(SSO_GET_TOKEN_URL,
                      headers={"User-Agent": common.UA, "Content-Type": "application/json",
                               "ProjectId": "erp", "cluster-id": "1", "lang": "cn",
                               "TimeZone": "UTC+8"},
                      data=json.dumps({"key": key, "lang": "zh"}), timeout=30)
    d = _parse_json(r)
    token = (d.get("data") or {}).get("token")
    if d.get("code") != 200 or not token:
        raise RuntimeError(f"getTokenByKey 续期失败: {str(d)[:150]}")
    from . import credentials
    c = credentials.get()
    mb = c.data.setdefault("mabang", {})
    mb["api_bearer"] = token
    mb["api_key"] = key
    with open(c.path, "w", encoding="utf-8") as f:
        json.dump(c.data, f, ensure_ascii=False, indent=2)
    c.reload()
    return token


def _cookie_value(www_cookie, name):
    m = re.search(re.escape(name) + r"=([^;]+)", www_cookie or "")
    return m.group(1) if m else ""


def _www_headers(cred):
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.mabangerp.com",
        "Referer": "https://www.mabangerp.com/index.php?mod=order.list",
        "Cookie": cred["www_cookie"],
    }


def _api_headers(cred):
    bearer = cred.get("api_bearer") or ""
    if not bearer:
        # 无 Bearer 时自动续期（用 www cookie 提取的 key 换发）
        bearer = refresh_api_token(cred)
    key = cred.get("api_key") or _api_key_from_www(cred)
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


def _api_post(cred, path, body):
    """api 域 POST：401 时自动续期 Bearer 后重试一次"""
    payload = json.dumps(body)
    for attempt in (1, 2):
        r = requests.post(API_BASE + path, headers=_api_headers(cred),
                          data=payload, timeout=60)
        if r.status_code != 401:
            return r
        if attempt == 2:
            break
        print("  [api] 401，自动续期 Bearer 后重试...")
        refresh_api_token(cred)
        cred = _mabang_cred()
    return r


def _parse_json(resp):
    """马帮 www 接口虽是 text/html mime 但返回 JSON"""
    return resp.json()


# ---------------- 本地真源表 ----------------
def load_local_mapping():
    """返回 (vc2中文名, 中文名→库存SKU)；读 data/ 价格映射表 + 商品价格表"""
    from openpyxl import load_workbook
    wb = load_workbook(config.MAPPING_XLSX, read_only=True)
    ws = wb["映射总表"]
    vc2cn = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1]:
            vc2cn[str(r[1]).strip()] = (str(r[0]).strip() if r[0] else "")
    wb.close()

    wb = load_workbook(config.BOSS_XLSX, read_only=True)
    ws = wb[wb.sheetnames[0]]
    cn2sku = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[2] and r[7]:
            cn2sku[str(r[2]).strip()] = str(r[7]).strip()
    wb.close()
    return vc2cn, cn2sku


# ---------------- 马帮接口 ----------------
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
        "global_company_config_json_str": "", "endPageNum": "",
    }
    orders, page = [], 1
    while page <= max_pages:
        form = dict(base_form)
        form["page"] = str(page)
        r = requests.post(WWW_BASE, params={"mod": "order.oTc"}, headers=_www_headers(cred),
                          data=urllib.parse.urlencode(form), timeout=60)
        r.raise_for_status()
        d = _parse_json(r)
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
    """查询全部状态订单（order.oTc 全量变体：isNewOrder=2 / a=orderalllist / tabId=222）。
    ⚠ 实测该变体忽略服务端日期过滤（游标式全量列表），日期过滤由调用方本地按 paidTime 做。
    自动翻页取全量（500/页，max_pages 限流 60 页=3 万单）。"""
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
        r = requests.post(WWW_BASE, params={"mod": "order.oTc"}, headers=_www_headers(cred),
                          data=urllib.parse.urlencode(form), timeout=120)
        r.raise_for_status()
        d = _parse_json(r)
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


def parse_order(o):
    """从订单行提取 摘要字段：订单ID/平台单号/店铺/VC/已匹配SKU/预报与交运状态标记"""
    oid = o.get("id")
    other = o.get("order_ellipsis_other_title") or ""   # 平台 SKU（vendorCode）
    matched = o.get("order_ellipsis_title") or ""       # 系统已匹配库存 SKU
    m_sk = TITLE_SKU_RE.search(matched)
    # 剥掉尾部 *N 数量后缀（如 HSJF-6161*1 → HSJF-6161）
    matched_sku = re.sub(r"\*\d+$", "", m_sk.group(1)).strip() if m_sk else ""
    m_vc = VC_RE.search(TITLE_SKU_RE.search(other).group(1).strip() if TITLE_SKU_RE.search(other) else "")
    vc = m_vc.group(1).strip() if m_vc else ""
    label = o.get("order_label") or ""
    lg_html = o.get("cansend1logisticsHtml") or ""
    return {
        "order_id": oid,
        "platform_order_id": o.get("platformOrderId"),
        "shop": o.get("shopIdText"),
        "paid_time": o.get("paidTime"),
        "vc": vc,
        "matched_sku": matched_sku,
        "kind": (re.search(r"商品种类:(\d+)", matched).group(1)
                 if re.search(r"商品种类:(\d+)", matched) else "?"),
        # 状态标记：order_label 含「已预报」= 已生成预报批次
        "has_forecast": "已预报" in label,
        # cansend1logisticsHtml 含 logisticsChannelText = 交运方式已选择
        "channel_selected": "logisticsChannelText" in lg_html,
    }


def fetch_order_item_ids(cred, order_id):
    """order.showOrderItems → 该订单的 orderItemId 列表（stock_data 键）"""
    form = {"orderItemIq": order_id, "tableBase": "1", "source": "common",
            "cloudStatus": "", "orderItemBy": "platformSku asc,isCombo asc,id asc,stockId asc",
            "tabId": "7"}
    r = requests.post(WWW_BASE, params={"mod": "order.showOrderItems"},
                      headers=_www_headers(cred), data=form, timeout=60)
    r.raise_for_status()
    d = _parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"showOrderItems 返回失败: {d.get('message')}")
    ids = list((d.get("stock_data") or {}).keys())
    return ids


def search_stock(cred, sku, warehouse_id, page_size=20):
    """按库存 SKU 精确搜索（searchStockList like 匹配后本地过滤精确相等），返回 stock 条目或 None"""
    body = {"skuType": 1, "operate": "like", "stockWarehouseId": str(warehouse_id),
            "pageSize": page_size, "page": 1, "warehouseIds": [], "searchValue": sku,
            "status": [1, 2, 3, 4, 5], "stockSkuOperate": "like",
            "stockNameCNOperate": "like", "stockNameENOperate": "like",
            "warehouseOperate": "like"}
    r = _api_post(cred, "/product/Stock/searchStockList", body)
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
    r = _api_post(cred, "/order/order/replaceOrderItem", body)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 200:
        raise RuntimeError(f"replaceOrderItem 失败: {d.get('msg') or d.get('message')}")
    return d


# ---------------- 预报批次（生成 + 上传） ----------------
DEF_FORECAST_LOGISTICS = "2369||262534"   # Wildberries线上发货（getForecastLogistics 默认）
DEF_FORECAST_CHANNEL = "wb_box"           # wildberries组包服务+打印箱贴(MP)


def _aamz_headers(cred):
    """aamz 域：实测接受 www 登录态（MABANG_ERP_PRO_MEMBERINFO_LOGIN_COOKIE），优先
    独立 aamz_cookie，未配置时回退 www_cookie"""
    return {
        "User-Agent": common.UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://aamz.mabangerp.com",
        "Referer": "https://aamz.mabangerp.com/index.php?mod=uploadforecastorderv2",
        "Cookie": cred.get("aamz_cookie") or cred["www_cookie"],
    }


def get_forecast_logistics(cred):
    """① 物流方式与默认预报参数（优先 createForecastSet 动态取，回退硬编码）"""
    r = requests.post(WWW_BASE, params={"mod": "ordera.getForecastLogistics"},
                      headers=_www_headers(cred), data="", timeout=60)
    r.raise_for_status()
    d = _parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"getForecastLogistics 返回失败: {d.get('message')}")
    s = d.get("createForecastSet") or {}
    logistics = s.get("platformForecastLogistics") or s.get("forecastLogistics") or DEF_FORECAST_LOGISTICS
    channel = s.get("platformForecastLogisticsChannel") or s.get("forecastLogisticsChannel") or DEF_FORECAST_CHANNEL
    return logistics, channel


def batch_create_forecast(cred, order_ids, logistics, channel):
    """② 生成预报批次（orderIds=平台单号逗号串），服务端按店铺自动拆批；返回批次号列表"""
    form = {
        "tablebase": "1", "orderIds": ",".join(str(x) for x in order_ids),
        "forecastLogistics": logistics, "forecastLogisticsChannel": channel,
        "create_forecast_set": "2", "smtCrossStorePackVal": "1",
        "tiktokPackType": "tiktok_pickup", "tiktokCrossStorePack": "1",
        "wildberriesCrossPack": "1", "activeTab": "platform",
    }
    r = requests.post(WWW_BASE, params={"mod": "ordera.doBatchCreateForecast"},
                      headers=_www_headers(cred),
                      data=urllib.parse.urlencode(form), timeout=120)
    r.raise_for_status()
    d = _parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"doBatchCreateForecast 返回失败: {d.get('message')}")
    batch_nos = [x for x in (d.get("batchNoStr") or d.get("batchNo") or "").split(",") if x]
    return batch_nos, d.get("message") or ""


def get_forecast_list(cred, status=1, rows_per_page=50):
    """③ 待上传/历史预报批次列表（aamz 域）；返回 (orderList, 统计dict)"""
    form = {"searchType": "1", "createoperType": "", "printstatus": "", "cancelStatus": "",
            "numberId": "", "datepicker-from": "", "datepicker-to": "", "status": str(status),
            "uploadlogisticschannel": "", "type": "1", "page": "1",
            "rowsPerPage": str(rows_per_page)}
    r = requests.post(AAMZ_BASE, params={"mod": "uploadforecastorderv2.getForecastOrderList"},
                      headers=_aamz_headers(cred), data=form, timeout=60)
    r.raise_for_status()
    d = _parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"getForecastOrderList 返回失败: {d.get('message')}")
    stats = {k: d.get(k) for k in ("waitTotal", "succesTotal", "failTotal", "historyTotal")}
    return d.get("orderList") or [], stats


def get_forecast_config(cred, my_logistics_id="262534"):
    """取上传预报单配置模板（aamz 域）：返回 forecast_config_json dict 或 None"""
    r = requests.post(AAMZ_BASE,
                      params={"mod": "uploadforecastorderv2.getForecastConfig"},
                      headers=_aamz_headers(cred),
                      data={"myLogisticsId": my_logistics_id}, timeout=60)
    r.raise_for_status()
    d = _parse_json(r)
    return d.get("forecast_config_json") if d.get("success") else None


def upload_forecast_batch(cred, batch_nos):
    """④ 依次上传预报批次（aamz 域，异步队列，勾选自动发货 wb_automark=1）。
    每批：getForecastConfig 取模板 → 覆盖 batchNoInfo/追加 wb_automark=1、
    is_set_forecast=2 → uploadForecastBatch（单批次号）。config 失败时回退
    旧固定表单（forecastLogistics=100，不勾自动发货）。返回 message 列表"""
    msgs = []
    for i, batch in enumerate(batch_nos):
        form = get_forecast_config(cred) or {}
        automark = bool(form)
        if form:
            form["batchNoInfo"] = batch
            form["wb_automark"] = "1"
            form["is_set_forecast"] = "2"
        else:
            # 回退：旧固定表单（与 2026-09-07 批量上传一致）
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
        r = requests.post(AAMZ_BASE,
                          params={"mod": "uploadforecastorderv2.uploadForecastBatch"},
                          headers=_aamz_headers(cred), data=form, timeout=120)
        r.raise_for_status()
        d = _parse_json(r)
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
# order.list 页面内嵌 JS 渠道数组：{"id":"830556","source":"1","logisticsId":"3048",
# "myLogisticsId":"262535","logisticsChannelName":"七库海外仓","logisticsName":"莫斯科仓-七库海外仓",...}
CHANNEL_OBJ_RE = re.compile(
    r'\{"id":"(\d+)","source":"\d+","logisticsId":"(\d+)","myLogisticsId":"(\d+)",'
    r'"logisticsChannelName":"((?:\\u[0-9a-fA-F]{4})+)"')


def discover_handover_channel(cred):
    """从 order.list 页面内嵌渠道数组动态发现交运渠道（按 handover_keyword 匹配），
    返回 channel_value 字符串（{id}_{myLogisticsId}_{名称}_{logisticsId}），失败返回 None"""
    keyword = cred.get("handover_keyword") or "七库海外仓"
    kw_esc = keyword.encode("unicode_escape").decode("latin-1")
    r = requests.get(WWW_BASE, params={"mod": "order.list", "Order_orderStatus": "2"},
                     headers={**_www_headers(cred), "Content-Type": ""}, timeout=120)
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
    """解析交运渠道完整值：① order.list 页面渠道数组动态发现（主链路）
    ② 配置渠道 id + getReportingInformation 解析  ③ 配置 handover_channel_value 兜底"""
    keyword = cred.get("handover_keyword") or "七库海外仓"
    # ① 动态发现（order.list 页面 4MB，含全部我方物流渠道，主链路）
    v = discover_handover_channel(cred)
    if v:
        return v
    print("  [提示] order.list 动态发现未命中，转配置渠道 id 接口解析...")
    # ② 配置渠道 id + getReportingInformation
    channel_id = cred.get("handover_channel_id") or ""
    if channel_id:
        try:
            form = {"orderId": str(sample_order_id), "tableBase": "1",
                    "myLogisticsChannelId": channel_id, "orderLogisticsSearchId": ""}
            r = requests.post(WWW_BASE, params={"mod": "order.getReportingInformation"},
                              headers=_www_headers(cred), data=form, timeout=60)
            r.raise_for_status()
            d = _parse_json(r)
            if d.get("success"):
                for v in re.findall(r"val\('([^']*)'\)",
                                    d.get("reportingInformationMyLogisticsChannelHtml") or ""):
                    if keyword in v:
                        return v
            print("  [提示] getReportingInformation 未解析到渠道选项，转配置兜底...")
        except Exception as e:
            print(f"  [提示] getReportingInformation 失败({str(e)[:60]})，转配置兜底...")
    # ③ 配置兜底
    fallback = cred.get("handover_channel_value") or ""
    if fallback:
        print(f"  [警告] 动态发现失败，回退配置值 {fallback}")
        return fallback
    raise RuntimeError(f"未能确定交运渠道值（渠道数组/渠道 id/配置均未命中「{keyword}」）")


def set_handover(cred, order_ids, channel_value):
    """提交物流交运（doReportingInformation）；返回 (成功订单id列表, 未找到列表)"""
    form = {"myLogisticsChannelId": channel_value,
            "orderId": ",".join(str(x) for x in order_ids),
            "source": "3", "trackNumber": "", "tableBase": "1",
            "quickaccessBol": "2", "dataChanges": "1", "isSyncLogistics": "1"}
    r = requests.post(WWW_BASE, params={"mod": "order.doReportingInformation"},
                      headers=_www_headers(cred),
                      data=urllib.parse.urlencode(form), timeout=120)
    r.raise_for_status()
    d = _parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"doReportingInformation 返回失败: {d.get('message')}")
    ok_ids = {str(x) for x in (d.get("successMessage") or [])}
    not_found = [str(x) for x in (d.get("notFoundPlatformOrderIds") or [])]
    return ok_ids, not_found


# ---------------- 主流程 ----------------
def classify(orders, vc2cn, cn2sku, shop_map):
    """给每个订单解析 VC → 中文名 → 期望SKU；非目标店铺标记 SKIP_SHOP，返回记录列表"""
    recs = []
    for o in orders:
        p = parse_order(o)
        rec = dict(p, cn_name="", expected_sku="", status="", note="",
                   shop_bcs=shop_map.get(p["shop"], ""))
        if not rec["shop_bcs"]:
            rec["status"] = "SKIP_SHOP"
            rec["note"] = f"非目标店铺={p['shop']}"
            recs.append(rec)
            continue
        if p["kind"] not in ("1", "?"):
            rec["status"] = "MULTI"
            rec["note"] = f"商品种类={p['kind']}，需人工处理"
            recs.append(rec)
            continue
        if not p["vc"].upper().startswith("BCS-"):
            rec["status"] = "NO_BCS"
            rec["note"] = "平台 SKU 非 BCS- 开头"
            recs.append(rec)
            continue
        cn = vc2cn.get(p["vc"])
        if cn is None:
            rec["status"] = "NO_VC"
            rec["note"] = "VC 不在映射表"
            recs.append(rec)
            continue
        rec["cn_name"] = cn
        sku = cn2sku.get(cn)
        if not sku:
            rec["status"] = "NO_SKU"
            rec["note"] = "中文名在商品价格表无库存SKU"
            recs.append(rec)
            continue
        rec["expected_sku"] = sku
        same = (sku.lower() == p["matched_sku"].lower())
        # ★ 用户规则：无论系统是否已匹配（含字符串一致），一律强制更换为 VC 链路查到的 SKU
        rec["status"] = "REPLACE"
        rec["note"] = f"系统匹配={p['matched_sku'] or '(空)'}；字符串{'一致' if same else '不一致'}"
        recs.append(rec)
    return recs


def apply_replace(cred, recs, warehouse_id):
    """对所有 REPLACE 订单强制执行更换（含系统匹配字符串一致的）；返回（成功数，失败数）"""
    ok = fail = 0
    for rec in recs:
        if rec["status"] != "REPLACE":
            continue
        try:
            ids = fetch_order_item_ids(cred, rec["order_id"])
            if len(ids) != 1:
                raise RuntimeError(f"orderItemId 数量={len(ids)}，需人工处理")
            stock = search_stock(cred, rec["expected_sku"], warehouse_id)
            if stock is None:
                raise RuntimeError(f"马帮未找到库存 SKU {rec['expected_sku']}")
            replace_order_item(cred, ids[0], stock["id"], warehouse_id)
            rec["note"] = (f"已更换 -> {stock['stockSku']}(stockId={stock['id']})")
            ok += 1
        except Exception as e:
            rec["note"] = f"更换失败: {str(e)[:80]}"
            fail += 1
        time.sleep(REQUEST_INTERVAL)
    return ok, fail


CSV_FIELDS = ["状态", "订单ID", "平台单号", "店铺", "付款时间", "VC码", "中文名",
              "系统匹配SKU", "期望SKU", "备注"]


def write_csv(recs):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"马帮订单匹配_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in recs:
            w.writerow({"状态": r["status"], "订单ID": r["order_id"],
                        "平台单号": r["platform_order_id"],
                        "店铺": (f"{r['shop_bcs']}({r['shop']})" if r["shop_bcs"] else r["shop"]),
                        "付款时间": r["paid_time"], "VC码": r["vc"],
                        "中文名": r["cn_name"], "系统匹配SKU": r["matched_sku"],
                        "期望SKU": r["expected_sku"], "备注": r["note"]})
    return path


def run(args):
    common.ensure_utf8_stdout()
    cred = _mabang_cred()
    warehouse_id = int(cred.get("warehouse_id") or 1457537)
    shop_map = cred["shop_map"]
    print(f"[配置] 目标店铺: {'、'.join(f'{k}→{v}' for k, v in shop_map.items())}")
    if args.apply and not _api_ready(cred):
        print("[错误] 更换订单商品需要 api 域凭证：请在 credentials.json 的 mabang.api_bearer "
              "填入 api.mabangerp.com 请求头 Authorization: Bearer 的值后重试")
        return 1

    print(f"\n[查询] 待处理订单（最近 {args.days} 天，平台=全部，tabId=7）...")
    orders = fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    if not orders:
        print("[完成] 无待处理订单")
        return 0

    vc2cn, cn2sku = load_local_mapping()
    recs = classify(orders, vc2cn, cn2sku, shop_map)

    stat = {}
    for r in recs:
        stat[r["status"]] = stat.get(r["status"], 0) + 1
    target_n = len(recs) - stat.get("SKIP_SHOP", 0)
    print(f"\n[匹配] 共 {len(recs)} 单：目标店铺 {target_n} 单，"
          f"跳过非目标店铺 {stat.get('SKIP_SHOP', 0)} 单；"
          + " / ".join(f"{k}={v}" for k, v in sorted(stat.items())))
    for r in recs:
        # SKIP_SHOP 只计汇总不逐行打印；REPLACE 为待执行项，dry-run 时全量展示
        if r["status"] not in ("SKIP_SHOP", "REPLACE") or not args.apply:
            print(f"  [{r['status']}] {r['order_id']} [{r['shop_bcs'] or r['shop']}] "
                  f"{r['vc'] or '(无VC)'} {r['cn_name']} | {r['note']}")

    if args.apply:
        n = stat.get("REPLACE", 0)
        print(f"\n[执行] 强制更换 {n} 个订单商品（含系统匹配一致者；仓库 {warehouse_id}）...")
        ok, fail = apply_replace(cred, recs, warehouse_id)
        print(f"  更换成功 {ok} / 失败 {fail}")
    else:
        print("\n（dry-run 模式未做任何更换；确认无误后加 --apply 执行）")

    report_no_sku(recs)

    csv_path = write_csv(recs)
    print(f"\n[汇总] 明细: {csv_path}")
    return 0


def report_no_sku(recs):
    """NO_SKU 明细报告（控制台 + CSV）：价格表缺库存 SKU 的订单需人工处理，
    仅做飞书登记/匹配，不进批次生成/上传/交运"""
    rows = [r for r in recs if r.get("status") == "NO_SKU"]
    if not rows:
        return
    print(f"\n[⚠ 需人工处理] 价格表缺库存 SKU 的订单 {len(rows)} 单"
          "（仅登记/匹配，未进批次生成/上传/交运）：")
    for r in rows:
        print(f"  {r['order_id']} [{r['shop_bcs'] or r['shop']}] "
              f"{r['vc'] or '(无VC)'} {r['cn_name']}")
    os.makedirs(config.LOG_DIR, exist_ok=True)
    path = os.path.join(config.LOG_DIR,
                        f"缺库存SKU订单_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["平台单号", "店铺", "中文名", "BCS编号", "说明"])
        for r in rows:
            w.writerow([r["order_id"], r["shop_bcs"] or r["shop"],
                        r["cn_name"], r["vc"], r["note"]])
    print(f"[报告] 明细: {path}")


# ---------------- 预报批次主流程 ----------------
FORECAST_CSV_FIELDS = ["类型", "订单ID", "平台单号", "店铺", "批次号", "数量",
                       "生成批次", "上传", "交运", "备注"]


def write_forecast_csv(order_recs, batch_rows):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"马帮预报批次_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FORECAST_CSV_FIELDS)
        w.writeheader()
        for r in order_recs:
            w.writerow({"类型": "订单", "订单ID": r["order_id"],
                        "平台单号": r["platform_order_id"],
                        "店铺": (f"{r['shop_bcs']}({r['shop']})" if r["shop_bcs"] else r["shop"]),
                        "批次号": r.get("_batch", ""), "数量": "",
                        "生成批次": r.get("_step_create", ""),
                        "上传": r.get("_step_upload", ""),
                        "交运": r.get("_step_handover", ""),
                        "备注": r["note"]})
        for b in batch_rows:
            w.writerow({"类型": "批次", "订单ID": "", "平台单号": "", "店铺": b["shop"],
                        "批次号": b["batchNo"], "数量": b["total"],
                        "生成批次": "", "上传": b.get("状态", ""),
                        "交运": "", "备注": b.get("note", "")})
    return path


def collect_forecast_orders(args):
    """查待处理订单 → 目标店铺 → VC 链路解析成功(REPLACE)的订单，返回 (recs, cred)"""
    cred = _mabang_cred()
    shop_map = cred["shop_map"]
    print(f"[配置] 目标店铺: {'、'.join(f'{k}→{v}' for k, v in shop_map.items())}")
    print(f"\n[查询] 待处理订单（最近 {args.days} 天）...")
    orders = fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    if not orders:
        print("[完成] 无待处理订单")
        return [], cred
    vc2cn, cn2sku = load_local_mapping()
    recs = classify(orders, vc2cn, cn2sku, shop_map)
    target = [r for r in recs if r["status"] == "REPLACE"]
    other = {}
    for r in recs:
        if r["status"] != "REPLACE":
            other[r["status"]] = other.get(r["status"], 0) + 1
    print(f"[圈定] 可预报（已匹配商品）{len(target)} 单；"
          f"排除: {' / '.join(f'{k}={v}' for k, v in sorted(other.items())) or '无'}")
    report_no_sku(recs)
    return target, cred


def run_forecast(args):
    common.ensure_utf8_stdout()
    cred_check = _mabang_cred()

    if args.check:
        print("[查询] 预报批次列表（status=1 待上传 + 统计）...")
        order_list, stats = get_forecast_list(cred_check)
        print(f"统计: 待上传 {stats.get('waitTotal')} / 成功 {stats.get('succesTotal')} / "
              f"失败 {stats.get('failTotal')} / 历史 {stats.get('historyTotal')}")
        for b in order_list:
            print(f"  {b['batchNo']} | {b.get('shopId')} | 状态={b.get('status')} "
                  f"订单数={b.get('total')} 成功={b.get('successNum')} 失败={b.get('failNum')} "
                  f"创建={b.get('createTime')}")
        print("\n（上传为异步处理，5-10 分钟后可重跑 --check 确认预报结果）")
        return 0

    target, cred = collect_forecast_orders(args)
    if not target:
        return 0

    # 幂等状态统计
    n_f = sum(1 for r in target if r["has_forecast"])
    n_c = sum(1 for r in target if r["channel_selected"])
    print(f"[状态] 已预报(已生成批次) {n_f} 单 / 未预报 {len(target) - n_f} 单；"
          f"交运方式已选 {n_c} 单 / 未选择 {len(target) - n_c} 单")
    for r in target:
        r.setdefault("_step_create", ""), r.setdefault("_step_upload", "")
        r.setdefault("_step_handover", ""), r.setdefault("_batch", "")

    if not args.apply:
        todo = [r for r in target if not r["has_forecast"]]
        print(f"\n将生成预报批次的平台单号 {len(todo)} 个（前 10 个）: "
              f"{','.join(str(r['platform_order_id']) for r in todo[:10])}"
              f"{'...' if len(todo) > 10 else ''}")
        print("\n（dry-run 模式未生成/上传/交运；确认无误后加 --apply 执行全链路）")
        return 0

    # ---- ① 生成预报批次（已预报的跳过） ----
    to_create = [r for r in target if not r["has_forecast"]]
    batch_nos_new = []
    if to_create:
        logistics, channel = get_forecast_logistics(cred)
        print(f"\n[① 生成批次] 物流={logistics} 渠道={channel}；"
              f"提交 {len(to_create)} 个平台单号...")
        pids = [str(r["platform_order_id"]) for r in to_create if r["platform_order_id"]]
        batch_nos_new, msg = batch_create_forecast(cred, pids, logistics, channel)
        print(f"  {msg}；新批次 {len(batch_nos_new)} 个: {','.join(batch_nos_new)}")
        for r in to_create:
            r["_step_create"] = "已生成"
            r["_step_upload"] = "已提交上传"
    else:
        print("\n[① 生成批次] 全部订单已预报，跳过（不重复生成）")
        for r in target:
            r["_step_create"] = "跳过-已预报"

    # ---- ② 上传批次（只传本次新生成的；已上传批次不在待上传列表，天然幂等） ----
    if batch_nos_new:
        up_msg = upload_forecast_batch(cred, batch_nos_new)
        print(f"[② 上传批次] {len(batch_nos_new)} 个新批次: {up_msg}")
    elif args.upload_waiting:
        waiting, _stats = get_forecast_list(cred, status=1)
        wb_nos = [b["batchNo"] for b in waiting]
        if wb_nos:
            up_msg = upload_forecast_batch(cred, wb_nos)
            print(f"[② 上传批次] 补传待上传列表 {len(wb_nos)} 个批次: {up_msg}")
            for r in target:
                r["_step_upload"] = r["_step_upload"] or "补传历史批次"
        else:
            print("[② 上传批次] 待上传列表为空，无需补传")
    else:
        print("[② 上传批次] 无新生成批次（历史批次已上传或用 --upload-waiting 补传），跳过")
        for r in target:
            r["_step_upload"] = r["_step_upload"] or "跳过-无新批次"

    # ---- ③ 等待系统更新（2-3 分钟，--wait 可覆盖） ----
    wait_s = int(getattr(args, "wait", 0) or cred.get("handover_wait_seconds") or 150)
    to_handover = [r for r in target if not r["channel_selected"]]
    if batch_nos_new and to_handover:
        print(f"\n[③ 等待] 系统信息更新 {wait_s}s（约 2-3 分钟）...")
        for left in range(wait_s, 0, -30):
            print(f"  剩余 {left}s ...")
            time.sleep(min(left, 30))
    elif to_handover:
        print(f"\n[③ 等待] 本次未生成新批次，仍等待 {wait_s}s 后尝试交运（--wait 可调）...")
        for left in range(wait_s, 0, -30):
            print(f"  剩余 {left}s ...")
            time.sleep(min(left, 30))
    else:
        print("\n[③ 等待] 所有订单交运方式已选择，跳过")

    # ---- ④ 设置物流交运方式（已选择的跳过） ----
    if not to_handover:
        print("[④ 交运] 全部订单已选择交运方式，跳过")
        for r in target:
            r["_step_handover"] = r["_step_handover"] or "跳过-已选择"
    else:
        print(f"[④ 交运] 对 {len(to_handover)} 个未选择交运方式的订单设置 "
              f"「{cred.get('handover_keyword') or '七库海外仓'}」...")
        try:
            channel_value = get_handover_channel_value(cred, to_handover[0]["order_id"])
            print(f"  交运渠道值: {channel_value}")
            ok_ids, not_found = set_handover(
                cred, [str(r["order_id"]) for r in to_handover], channel_value)
            for r in to_handover:
                if str(r["order_id"]) in ok_ids:
                    r["_step_handover"] = "交运成功"
                else:
                    r["_step_handover"] = "交运失败-未在成功列表"
            if not_found:
                print(f"  [警告] 平台未找到订单: {not_found}")
            print(f"  交运成功 {len(ok_ids)} / 共提交 {len(to_handover)}")
        except Exception as e:
            print(f"  [错误] 交运失败: {e}")
            for r in to_handover:
                r["_step_handover"] = f"错误:{str(e)[:40]}"

    # ---- 批次清单回读 ----
    batch_rows = []
    try:
        order_list, _stats = get_forecast_list(cred, status=1)
        for b in order_list:
            if b["batchNo"] in batch_nos_new:
                batch_rows.append({"batchNo": b["batchNo"], "shop": b.get("shopId"),
                                   "total": b.get("total"), "状态": f"status={b.get('status')}"})
    except Exception as e:
        print(f"  [警告] 回读批次清单失败: {e}")

    csv_path = write_forecast_csv(target, batch_rows)
    print(f"\n[汇总] 订单 {len(target)} 单（生成 {len(to_create)}/{len(target)}，"
          f"交运 {sum(1 for r in target if r['_step_handover'] == '交运成功')}）"
          f"| 批次 {len(batch_nos_new)} 个 | 明细: {csv_path}")
    print("（上传为异步处理，5-10 分钟后运行 wb.py mabang-forecast --check 确认预报结果）")
    return 0

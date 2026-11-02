"""forecast。"""

import re
import time
import urllib.parse
import requests


from sellerops.integrations.mabang.client import AAMZ_BASE
from sellerops.integrations.mabang.client import CHANNEL_OBJ_RE
from sellerops.integrations.mabang.client import DEF_FORECAST_CHANNEL
from sellerops.integrations.mabang.client import DEF_FORECAST_LOGISTICS
from sellerops.integrations.mabang.client import WWW_BASE
from sellerops.integrations.mabang.client import aamz_headers
from sellerops.integrations.mabang.client import parse_json
from sellerops.integrations.mabang.client import www_headers

FORECAST_PAGE_TOTAL_RE = re.compile(r"共\s*(\d+)\s*条")

FORECAST_PAGE_RANGE_RE = re.compile(r"第\s*(\d+)\s*-\s*(\d+)\s*条")

FORECAST_PAGE_NO_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*页")

def parse_forecast_page(page_html):
    """解析预报批次列表的 pageHtml → {'pageTotal','pageFrom','pageTo','pageCur','pageCount'}。

    pageHtml 是带标签的 HTML（数字常被 <span> 等分隔），先剥标签再按空白归一化匹配；
    形如 '<b>共 200 条 当前显示第 1-50 条 1/4 页</b>'。空列表时 pageHtml 为空串 ⇒ 全 0。
    用于分页终止与「共 N 条」展示。
    """
    html = re.sub(r"<[^>]+>", " ", page_html or "")
    text = " ".join(html.split())
    m = FORECAST_PAGE_TOTAL_RE.search(text)
    total = int(m.group(1)) if m else 0
    m = FORECAST_PAGE_RANGE_RE.search(text)
    begin, end = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    m = FORECAST_PAGE_NO_RE.search(text)
    cur, pages = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return {"pageTotal": total, "pageFrom": begin, "pageTo": end,
            "pageCur": cur, "pageCount": pages}

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

def get_forecast_list(cred, status=1, rows_per_page=200, page=1):
    """预报批次列表（aamz 域）；返回 (orderList, stats)。

    tab → status：待预报=1 / 预报成功=3 / 预报失败=99 / 历史预报=5（2026-09-30 抓包实证）。
    stats = 全局四统计（waitTotal/succesTotal/failTotal/historyTotal，**不随 tab 变**）
            + pageHtml 解析键（pageTotal/pageFrom/pageTo/pageCur/pageCount）+ pageHtml 原文。
    批次行关键字段：batchNo(揽货批次号)/shopId(=shop_map 键)/status(1待上传 2上传中 3成功 99失败)
                    /isHistoryStatus(当前 tab)/total/successNum/failNum/handoverNumber(成功后回填)
                    /createTime/employeeName/forecastLogisticsName(上传货代)/orderWeight(批次重量)。
    注意：orderList **无订单级明细**（无 orderIds/子列表）⇒ 映射只能到批次级 + 店铺级。
    """
    form = {"searchType": "1", "createoperType": "", "printstatus": "", "cancelStatus": "",
            "numberId": "", "datepicker-from": "", "datepicker-to": "", "status": str(status),
            "uploadlogisticschannel": "", "type": "1", "page": str(page),
            "rowsPerPage": str(rows_per_page)}
    r = requests.post(AAMZ_BASE, params={"mod": "uploadforecastorderv2.getForecastOrderList"},
                      headers=aamz_headers(cred), data=form, timeout=60)
    r.raise_for_status()
    d = parse_json(r)
    if not d.get("success"):
        raise RuntimeError(f"getForecastOrderList 返回失败: {d.get('message')}")
    stats = {k: d.get(k) for k in ("waitTotal", "succesTotal", "failTotal", "historyTotal")}
    stats.update(parse_forecast_page(d.get("pageHtml")))
    stats["pageHtml"] = d.get("pageHtml") or ""
    return d.get("orderList") or [], stats

def get_forecast_config(cred, my_logistics_id="262534"):
    """取上传预报单配置模板（aamz 域）"""
    r = requests.post(AAMZ_BASE, params={"mod": "uploadforecastorderv2.getForecastConfig"},
                      headers=aamz_headers(cred), data={"myLogisticsId": my_logistics_id}, timeout=60)
    r.raise_for_status()
    d = parse_json(r)
    return d.get("forecast_config_json") if d.get("success") else None

def upload_forecast_batch(cred, batch_nos):
    """依次上传预报批次（aamz 域，异步队列，勾选自动发货 wb_automark=1）。

    优化（2026-09-30 抓包实测）：getForecastConfig 模板与具体批次无关，**循环外只取一次**
    （原实现每批取一次），循环内基于模板副本覆盖 batchNoInfo，避免跨批污染。
    config 取不到（None）⇒ 整轮降级为旧批量方式（forecastLogistics=100，**失去自动发货**），
    与原「form 为空即降级」语义等价。
    上传响应仅 `{"success":true,"message":"批次已插入上传队列，请5-10分后查看预报结果"}`
    —— **无任务 id / 无进度字段**，完成判定必须依赖 wait_upload_done 的列表轮询。
    """
    msgs = []
    template = get_forecast_config(cred)      # 只取一次（循环外）
    automark = bool(template)
    if not automark:
        print("  [警告] 获取上传配置模板失败，本次降级为 forecastLogistics=100（不勾选自动发货）")
    for i, batch in enumerate(batch_nos):
        if automark:
            form = dict(template)             # 模板副本（浅拷贝足够：仅覆盖标量键）
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

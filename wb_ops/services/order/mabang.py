# -*- coding: utf-8 -*-
"""
wb_ops 马帮 ERP 待处理订单 SKU 匹配更换与预报交运协调器

业务流程：
  1. 查询马帮待处理订单列表（Wildberries 平台）
  2. 逐单从平台 SKU 提取 VC 码（BCS-{前缀}-{nmId}）→ 映射表查中文名 → 商品价格表查库存 SKU
  3. 待更换订单标记与比对
  4. 更换：调用 adapters.mabang_client 提交 replaceOrderItem
  5. 预报与交运全链路流转编排

底层 HTTP 接口交互由 wb_ops.adapters.mabang_client 统一托管。
"""
import csv
import json
import os
import re
import time
import requests

from wb_ops import config
from wb_ops import common
from wb_ops.adapters import mabang_client as mb_client

# 重新导出常量与底层接口，保持 100% 向后兼容
WWW_BASE = mb_client.WWW_BASE
AAMZ_BASE = mb_client.AAMZ_BASE
API_BASE = mb_client.API_BASE
SSO_GET_TOKEN_URL = mb_client.SSO_GET_TOKEN_URL
REQUEST_INTERVAL = mb_client.REQUEST_INTERVAL
PLATFORM_ID_WB = mb_client.PLATFORM_ID_WB
DEF_FORECAST_LOGISTICS = mb_client.DEF_FORECAST_LOGISTICS
DEF_FORECAST_CHANNEL = mb_client.DEF_FORECAST_CHANNEL
CHANNEL_OBJ_RE = mb_client.CHANNEL_OBJ_RE

_mabang_cred = mb_client.get_mabang_cred
_cookie_value = mb_client.cookie_value
_api_key_from_www = mb_client.api_key_from_www
_api_ready = mb_client.api_ready
refresh_api_token = mb_client.refresh_api_token
_www_headers = mb_client.www_headers
_api_headers = mb_client.api_headers
_aamz_headers = mb_client.aamz_headers
_parse_json = mb_client.parse_json
_api_post = mb_client.api_post
fetch_pending_orders = mb_client.fetch_pending_orders
fetch_all_orders = mb_client.fetch_all_orders
fetch_order_item_ids = mb_client.fetch_order_item_ids
search_stock = mb_client.search_stock
replace_order_item = mb_client.replace_order_item
get_forecast_logistics = mb_client.get_forecast_logistics
batch_create_forecast = mb_client.batch_create_forecast
get_forecast_list = mb_client.get_forecast_list
parse_forecast_page = mb_client.parse_forecast_page
get_forecast_config = mb_client.get_forecast_config
upload_forecast_batch = mb_client.upload_forecast_batch
discover_handover_channel = mb_client.discover_handover_channel
get_handover_channel_value = mb_client.get_handover_channel_value
set_handover = mb_client.set_handover

VC_RE = re.compile(r"(BCS-[A-Z]{4}-(?:(?:ozon-card-)?[A-Za-z0-9-]+|[^/*\s]+/\d+))(?:\*\d+)?$")
SKU_RE = re.compile(r"商品编号\*数量:(.+?)\*?\d*$")
TITLE_SKU_RE = re.compile(r"数量:(.*)$")


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


def parse_order(o):
    """从订单行提取摘要字段：订单ID/平台单号/店铺/VC/已匹配SKU/预报与交运状态标记"""
    oid = o.get("id")
    other = o.get("order_ellipsis_other_title") or ""   # 平台 SKU（vendorCode）
    matched = o.get("order_ellipsis_title") or ""       # 系统已匹配库存 SKU
    m_sk = TITLE_SKU_RE.search(matched)
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
        "has_forecast": "已预报" in label,
        "channel_selected": "logisticsChannelText" in lg_html,
    }


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
    """对所有 REPLACE 订单强制执行更换；返回（成功数，失败数）"""
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
            rec["note"] = f"已更换 -> {stock['stockSku']}(stockId={stock['id']})"
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


def report_no_sku(recs):
    """NO_SKU 明细报告（控制台 + CSV）：价格表缺库存 SKU 的订单需人工处理"""
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


# ---------------- 预报批次主流程 ----------------
FORECAST_CSV_FIELDS = ["类型", "订单ID", "平台单号", "店铺", "批次号", "数量",
                       "生成批次", "上传", "交运", "备注",
                       "物流跟踪号", "批次重量", "上传货代"]


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
                        "交运": "", "备注": b.get("note", ""),
                        "物流跟踪号": b.get("handoverNumber", ""),
                        "批次重量": b.get("orderWeight", ""),
                        "上传货代": b.get("forecastLogisticsName", "")})
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
    # 已取消订单排除（WB 门户取消单；马帮不同步取消单，防御性过滤）
    from wb_ops.adapters import wb_client as wb_api
    canceled = set()
    for label in shop_map.values():
        m = re.search(r"\((\d+)\)", label)
        if not m:
            continue
        try:
            canceled |= wb_api.fetch_canceled_ids(int(m.group(1)))
        except Exception as e:
            print(f"  [警告] 店{m.group(1)} 取消单查询失败（跳过排除）: {e}")
    n_cancel = sum(1 for r in target if str(r["platform_order_id"]) in canceled)
    if n_cancel:
        target = [r for r in target if str(r["platform_order_id"]) not in canceled]
        other["已取消"] = other.get("已取消", 0) + n_cancel
    print(f"[圈定] 可预报（已匹配商品）{len(target)} 单；"
          f"排除: {' / '.join(f'{k}={v}' for k, v in sorted(other.items())) or '无'}")
    report_no_sku(recs)
    return target, cred


DEF_UPLOAD_TIMEOUT = 900      # 等待批次上传结束的最长秒数（--upload-timeout / 配置可覆盖）
DEF_POLL_INTERVAL = 20        # 批次状态轮询间隔秒数（--poll-interval / 配置可覆盖）


def _resolve_upload_timeout(args, cred):
    """上传完成轮询超时（秒）：CLI --upload-timeout > cred.mabang.upload_timeout_seconds > 900"""
    v = getattr(args, "upload_timeout", None)
    if v is None:
        v = cred.get("upload_timeout_seconds")
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEF_UPLOAD_TIMEOUT


def _resolve_poll_interval(args, cred):
    """批次状态轮询间隔（秒）：CLI --poll-interval > cred.mabang.upload_poll_interval > 20"""
    v = getattr(args, "poll_interval", None)
    if v is None:
        v = cred.get("upload_poll_interval")
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEF_POLL_INTERVAL


def _ceil_pages(total, size):
    """按总数与页大小算页数；参数非法返回 0。"""
    try:
        total, size = int(total), int(size)
    except (TypeError, ValueError):
        return 0
    return (total + size - 1) // size if total > 0 and size > 0 else 0


def find_batches(cred, batch_nos, status, rows_per_page=200, max_pages=50):
    """在指定 status 列表中检索 batch_nos；首批未命中则按 pageHtml 自动翻页。

    Args:
        status: 1=待预报 / 3=预报成功 / 99=预报失败 / 5=历史预报。
        max_pages: 翻页上限（防异常时无限翻页）。

    Returns:
        {"found": {batchNo: row}, "missing": set, "all_found": bool,
         "pages_fetched": int, "stats": dict}
    """
    want = {str(b) for b in (batch_nos or []) if b}
    res = {"found": {}, "missing": set(want), "all_found": not want,
           "pages_fetched": 0, "stats": {}}
    if not want:
        return res
    page, total_pages = 1, None
    while page <= max_pages:
        rows, stats = get_forecast_list(cred, status=status,
                                        rows_per_page=rows_per_page, page=page)
        res["pages_fetched"] = page
        res["stats"] = stats or {}
        for r in rows:
            bn = str(r.get("batchNo"))
            if bn in want:
                res["found"][bn] = r
        res["missing"] = want - set(res["found"])
        if not res["missing"]:
            res["all_found"] = True
            break
        if len(rows) < rows_per_page:          # 末页（不足一页）
            break
        if total_pages is None:                # 页数优先取 pageHtml，其次按总数折算
            total_pages = (res["stats"].get("pageCount")
                           or _ceil_pages(res["stats"].get("pageTotal"), rows_per_page))
        if total_pages and page >= total_pages:
            break
        page += 1
    return res


def _warn_batch_failnum(found_rows, warned):
    """批次行内 failNum>0 告警（每批次只告警一次）。"""
    for bn, row in (found_rows or {}).items():
        try:
            fn = int(row.get("failNum") or 0)
        except (TypeError, ValueError):
            fn = 0
        if fn and bn not in warned:
            warned.add(bn)
            print(f"  [⚠ 告警] 批次 {bn}[{row.get('shopId')}] 行内失败 {fn} 单"
                  f"（成功={row.get('successNum')}/{row.get('total')}），请核查")


def wait_upload_done(cred, batch_nos, timeout=DEF_UPLOAD_TIMEOUT,
                     interval=DEF_POLL_INTERVAL, min_wait=0, rows_per_page=200):
    """③ 轮询等待本批预报单真正上传结束（**正向判据**）。

    完成判据：batch_nos 全部命中 status=3「预报成功」；为防成功后迅速归档移入
    status=5「历史预报」导致永不命中，**命中 status=5 亦算成功**。
    依据（2026-09-30 抓包实证）：批次上传期间留在 status=1 列表（行级 status 1→2「上传中」），
    上传成功后出现在 status=3 列表（successNum==total、handoverNumber 回填）。
    每轮预算：status=3 一次；未成功者再查 status=1 判「仍在传」；既非成功也不在待上传的
    「未知」批次补查 status=5；超时后才查 status=99 定性失败。查询异常容错重试至超时。

    Returns:
        {"done": bool,           # True=全部批次已确认成功
         "success": set[str],    # 命中 status=3 或 5
         "failed": set[str],     # 超时定性命中 status=99
         "still": set[str],      # 超时仍未完成（未知态）
         "store_of": {bn: shopId},   # 店铺级映射（局部跳过交运用）
         "rows": {bn: row},      # 各批次最后快照
         "stats": dict, "reason": "ok"|"timeout"}
    """
    batch_nos = [str(b) for b in (batch_nos or []) if b]
    if not batch_nos:
        return {"done": True, "success": set(), "failed": set(), "still": set(),
                "store_of": {}, "rows": {}, "stats": {}, "reason": "ok"}
    want = set(batch_nos)
    success, failed, rows, store_of, last_stats = set(), set(), {}, {}, {}
    warned, prev_fail = set(), None
    start = time.time()
    mw = f"，最小等待 {min_wait}s" if min_wait else ""
    print(f"\n[③ 等待上传] 轮询 {len(batch_nos)} 个批次直至命中「预报成功(status=3)」"
          f"或「历史预报(status=5)」（超时 {timeout}s，间隔 {interval}s{mw}）...")
    while True:
        # ① status=3 预报成功：命中即成功
        try:
            r3 = find_batches(cred, want, status=3, rows_per_page=rows_per_page)
            if r3["stats"]:
                last_stats = r3["stats"]
            for bn, row in r3["found"].items():
                success.add(bn)
                rows[bn] = row
                store_of.setdefault(bn, row.get("shopId"))
            _warn_batch_failnum(r3["found"], warned)
        except Exception as e:
            print(f"  [警告] status=3 查询异常（{str(e)[:80]}），稍后重试 ...")

        # ② status=1 待预报：未成功者是否仍在上传/排队
        pend = want - success
        uploading = {}
        if pend:
            try:
                r1 = find_batches(cred, pend, status=1, rows_per_page=rows_per_page)
                if r1["stats"]:
                    last_stats = r1["stats"]
                uploading = r1["found"]
                for bn, row in uploading.items():
                    rows[bn] = row
                    store_of.setdefault(bn, row.get("shopId"))
                _warn_batch_failnum(uploading, warned)
            except Exception as e:
                print(f"  [警告] status=1 查询异常（{str(e)[:80]}），稍后重试 ...")

        # ③ 既非成功也不在待上传的「未知」批次：查 status=5 兜底（成功后归档）
        unknown = want - success - set(uploading)
        if unknown:
            try:
                r5 = find_batches(cred, unknown, status=5, rows_per_page=rows_per_page)
                if r5["stats"]:
                    last_stats = r5["stats"]
                for bn, row in r5["found"].items():
                    success.add(bn)
                    rows[bn] = row
                    store_of.setdefault(bn, row.get("shopId"))
                _warn_batch_failnum(r5["found"], warned)
            except Exception as e:
                print(f"  [警告] status=5 查询异常（{str(e)[:80]}），稍后重试 ...")

        fail_now = int((last_stats or {}).get("failTotal") or 0)
        if prev_fail is not None and fail_now > prev_fail:
            print(f"  [⚠ 告警] failTotal 上涨：{prev_fail} → {fail_now}"
                  f"（疑似批次上传失败，请留意）")
        prev_fail = fail_now

        still = want - success
        elapsed = int(time.time() - start)
        detail = "  ".join(
            (f"{bn}[{store_of.get(bn)}] status={rows[bn].get('status')} "
             f"{rows[bn].get('successNum')}/{rows[bn].get('total')}(fail={rows[bn].get('failNum')})"
             if bn in rows else f"{bn}（暂无快照）")
            for bn in sorted(still)) or "（无）"
        print(f"  [{elapsed}s/剩余 {max(0, timeout - elapsed)}s] 已成功 {len(success)}/{len(want)}"
              f"，未成功 {len(still)}: {detail}"
              f" | wait={last_stats.get('waitTotal')} succ={last_stats.get('succesTotal')}"
              f" fail={last_stats.get('failTotal')}")

        if not still and elapsed >= min_wait:
            print(f"  [完成] 全部 {len(want)} 个批次已确认预报成功（耗时 {elapsed}s）")
            return {"done": True, "success": success, "failed": set(), "still": set(),
                    "store_of": store_of, "rows": rows, "stats": last_stats, "reason": "ok"}
        if elapsed >= timeout:
            break
        time.sleep(interval)

    # 超时：查一次 status=99 定性「预报失败」，其余归入未完成
    try:
        r99 = find_batches(cred, want - success, status=99, rows_per_page=rows_per_page)
        for bn, row in r99["found"].items():
            failed.add(bn)
            rows[bn] = row
            store_of.setdefault(bn, row.get("shopId"))
    except Exception as e:
        print(f"  [警告] status=99 定性查询异常（{str(e)[:80]}），未成功批次按未完成处理")
    still = want - success - failed
    print(f"  [⚠ 超时] {int(timeout)}s 内：成功 {len(success)} / 失败 {len(failed)}"
          f" / 未完成 {len(still)}{('：' + ','.join(sorted(still))) if still else ''}")
    print("          失败批次订单将标记「跳过-预报失败」、未完成批次订单标记"
          "「跳过-上传未完成」，均不交运；稍后重跑本命令可幂等补齐。")
    return {"done": False, "success": success, "failed": failed, "still": still,
            "store_of": store_of, "rows": rows, "stats": last_stats, "reason": "timeout"}



FORECAST_STATUS_TAGS = {1: "待上传", 2: "上传中", 3: "预报成功", 99: "预报失败"}


def _print_forecast_rows(rows):
    """打印预报批次行（揽货批次号/批次状态/订单总数/批次重量/上传货代/创建人/回传号）。"""
    for b in rows:
        st = int(b.get("status") or 0)
        tag = FORECAST_STATUS_TAGS.get(st, f"状态{st}")
        fl = int(b.get("failNum") or 0)
        print(f"  {b.get('batchNo')} | {b.get('shopId')} | {tag}(status={st}) | "
              f"订单数={b.get('total')} 成功={b.get('successNum')} 失败={fl} | "
              f"货代={b.get('forecastLogisticsName') or '-'} | "
              f"创建人={b.get('employeeName') or '-'} | "
              f"回传号={b.get('handoverNumber') or '-'} | "
              f"批次重量={b.get('orderWeight') or '-'} | 创建={b.get('createTime')}")
        if fl:
            print(f"     [⚠ 告警] 该批次失败 {fl} 单，请核查")


def run_forecast(args):
    common.ensure_utf8_stdout()
    cred_check = _mabang_cred()

    if args.check:
        tabs = [(1, "待预报"), (3, "预报成功"), (99, "预报失败"), (5, "历史预报")]
        labels = dict(tabs)
        status = getattr(args, "status", None)
        pages = int(getattr(args, "pages", 1) or 0)

        if status:
            status = int(status)
            label = labels.get(status, f"status={status}")
            print(f"[查询] {label}(status={status}) 批次列表"
                  f"（{'翻到底' if pages == 0 else f'最多 {pages} 页'}，每页 200）...")
            rows_all, last = [], {}
            page, max_pages = 1, (99 if pages == 0 else pages)
            while page <= max_pages:
                try:
                    rows, st = get_forecast_list(cred_check, status=status,
                                                 rows_per_page=200, page=page)
                except Exception as e:
                    print(f"  [错误] 查询失败: {str(e)[:80]}")
                    return 1
                last = st or {}
                rows_all.extend(rows)
                if len(rows) < 200 or (last.get("pageCount") and page >= last["pageCount"]):
                    break
                page += 1
            print(f"  共 {last.get('pageTotal')} 条 第{last.get('pageFrom')}-{last.get('pageTo')}条 "
                  f"{last.get('pageCur')}/{last.get('pageCount')}页")
            if not rows_all:
                print("  （无）")
            else:
                _print_forecast_rows(rows_all)
            return 0

        print("[查询] 预报批次概览（待预报=1 / 预报成功=3 / 预报失败=99 / 历史预报=5）...")
        first, stats0 = {}, {}
        for st_code, _label in tabs:
            try:
                rows, st = get_forecast_list(cred_check, status=st_code,
                                             rows_per_page=200, page=1)
                first[st_code] = (rows, st or {})
                if not stats0:
                    stats0 = st or {}
            except Exception as e:
                first[st_code] = ([], {"_err": str(e)[:80]})
        print(f"统计: 待上传 {stats0.get('waitTotal')} / 成功 {stats0.get('succesTotal')} / "
              f"失败 {stats0.get('failTotal')} / 历史 {stats0.get('historyTotal')}")
        for st_code, label in tabs:
            rows, st = first.get(st_code, ([], {}))
            if st.get("_err"):
                print(f"\n▸ status={st_code} {label}   查询失败: {st['_err']}")
                continue
            print(f"\n▸ status={st_code} {label}   共 {st.get('pageTotal')} 条 "
                  f"第{st.get('pageFrom')}-{st.get('pageTo')}条 "
                  f"{st.get('pageCur')}/{st.get('pageCount')}页")
            if not rows:
                print("  （无）")
            else:
                _print_forecast_rows(rows[:3])
                if len(rows) > 3:
                    print(f"  ...（仅列前 3 行；完整明细请用 --status {st_code}）")
        return 0

    target, cred = collect_forecast_orders(args)
    if not target:
        return 0

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

    up_timeout = _resolve_upload_timeout(args, cred)
    poll_iv = _resolve_poll_interval(args, cred)
    min_wait = int(getattr(args, "wait", 0) or 0)
    uploaded_nos = []          # 本轮流经上传的批次号（新批次或补传批次）

    if batch_nos_new:
        up_msg = upload_forecast_batch(cred, batch_nos_new)
        print(f"[② 上传批次] {len(batch_nos_new)} 个新批次: {up_msg}")
        uploaded_nos = [str(b) for b in batch_nos_new]
    elif getattr(args, "upload_waiting", False):
        waiting, _stats = get_forecast_list(cred, status=1)
        wb_nos = [b["batchNo"] for b in waiting]
        if wb_nos:
            up_msg = upload_forecast_batch(cred, wb_nos)
            print(f"[② 上传批次] 补传待上传列表 {len(wb_nos)} 个批次: {up_msg}")
            for r in target:
                r["_step_upload"] = r["_step_upload"] or "补传历史批次"
            uploaded_nos = [str(b) for b in wb_nos]
        else:
            print("[② 上传批次] 待上传列表为空，无需补传")
    else:
        print("[② 上传批次] 无新生成批次（历史批次已上传或用 --upload-waiting 补传），跳过")
        for r in target:
            r["_step_upload"] = r["_step_upload"] or "跳过-无新批次"

    # ③ 轮询等待上传结束（正向判据：命中 status=3 预报成功；status=5 历史归档亦算成功）
    to_handover = [r for r in target if not r["channel_selected"]]
    skipped_fail, skipped_upload, up_rows, store_of = [], [], {}, {}
    success_nos, failed_nos, still_nos = set(), set(), set()
    if uploaded_nos:
        res = wait_upload_done(cred, uploaded_nos, timeout=up_timeout,
                               interval=poll_iv, min_wait=min_wait)
        up_rows, store_of = res["rows"], res["store_of"]
        success_nos, failed_nos, still_nos = res["success"], res["failed"], res["still"]

        fail_stores = {store_of.get(b) for b in failed_nos if store_of.get(b)}
        still_stores = {store_of.get(b) for b in still_nos if store_of.get(b)}
        fail_unknown = bool(failed_nos and not fail_stores)
        still_unknown = bool(still_nos and not still_stores)

        if failed_nos:      # 预报失败优先：该批次所属店铺本轮不交运
            if fail_unknown:
                skipped_fail = list(to_handover)
                print("  [警告] 无法定位失败批次所属店铺，本轮交运整体跳过（保守策略）")
            else:
                skipped_fail = [r for r in to_handover if r["shop"] in fail_stores]
            for r in skipped_fail:
                r["_step_handover"] = "跳过-预报失败"
            to_handover = [r for r in to_handover if r not in skipped_fail]

        if still_nos:       # 上传未完成：同样不交运
            if still_unknown or fail_unknown:
                skipped_upload = list(to_handover)
                print("  [警告] 无法定位未完成批次所属店铺，本轮交运整体跳过（保守策略）")
            else:
                skipped_upload = [r for r in to_handover if r["shop"] in still_stores]
            for r in skipped_upload:
                r["_step_handover"] = "跳过-上传未完成"
            to_handover = [r for r in to_handover if r not in skipped_upload]
    elif min_wait > 0:
        print(f"\n[③ 等待] 本次未生成新批次，额外等待 {min_wait}s（--wait）...")
        for left in range(min_wait, 0, -30):
            print(f"  剩余 {left}s ...")
            time.sleep(min(left, 30))
    else:
        print("\n[③ 等待] 本次未生成新批次，直接尝试交运（不轮询）")

    if skipped_fail:
        print(f"[⚠ 告警] {len(skipped_fail)} 单因「预报失败」跳过交运（标记=跳过-预报失败）；"
              f"请到马帮后台核查失败批次后重跑本命令。")
    if skipped_upload:
        print(f"[⚠ 告警] {len(skipped_upload)} 单因「上传未完成」跳过交运"
              f"（标记=跳过-上传未完成）；稍后重跑 mabang-process 幂等补交运。")

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

    batch_rows = []
    for bn in uploaded_nos:      # 优先用轮询快照（批次可在轮询后继续变化）
        b = (up_rows or {}).get(bn) or {}
        shop = b.get("shopId") or store_of.get(bn, "")
        state = ("预报成功" if bn in success_nos else
                 "预报失败" if bn in failed_nos else
                 "上传未完成" if bn in still_nos else "状态未知")
        status_txt = (f"{state}(status={b.get('status')} 成功={b.get('successNum')}/{b.get('total')} "
                      f"失败={b.get('failNum')})" if b else state)
        batch_rows.append({"batchNo": bn, "shop": shop, "total": b.get("total"),
                           "状态": status_txt, "note": "",
                           "handoverNumber": b.get("handoverNumber", ""),
                           "orderWeight": b.get("orderWeight", ""),
                           "forecastLogisticsName": b.get("forecastLogisticsName", "")})
    if batch_rows and not any(r.get("状态", "").startswith("预报成功") for r in batch_rows):
        try:                     # 无成功快照时回读 status=3 兜底
            r3 = find_batches(cred, batch_nos_new or uploaded_nos, status=3)
            for bn, b in r3["found"].items():
                for row in batch_rows:
                    if row["batchNo"] == bn:
                        row["状态"] = (f"预报成功(status={b.get('status')} "
                                       f"成功={b.get('successNum')}/{b.get('total')})")
                        row["handoverNumber"] = b.get("handoverNumber", "")
                        row["orderWeight"] = b.get("orderWeight", "")
                        row["forecastLogisticsName"] = b.get("forecastLogisticsName", "")
        except Exception as e:
            print(f"  [警告] 回读批次清单失败: {e}")

    csv_path = write_forecast_csv(target, batch_rows)
    n_ok = sum(1 for r in target if r["_step_handover"] == "交运成功")
    n_fail = sum(1 for r in target if r["_step_handover"] == "跳过-预报失败")
    n_skip = sum(1 for r in target if r["_step_handover"] == "跳过-上传未完成")
    bits = f"，跳过-预报失败 {n_fail}" if n_fail else ""
    bits += f"，跳过-上传未完成 {n_skip}" if n_skip else ""
    print(f"\n[汇总] 订单 {len(target)} 单（生成 {len(to_create)}/{len(target)}，"
          f"交运成功 {n_ok}{bits}）| 批次 {len(batch_nos_new)} 个"
          f"（成功 {len(success_nos)}/失败 {len(failed_nos)}/未完成 {len(still_nos)}）"
          f" | 明细: {csv_path}")
    if n_fail:
        print("[⚠ 告警] 存在预报失败批次：对应订单未交运；请到马帮后台核查后重跑 mabang-process。")
    if n_skip:
        print("[⚠ 告警] 存在上传未完成批次：对应订单未交运；稍后重跑 mabang-process 幂等补齐。")
    if not (n_fail or n_skip):
        print("（本次已轮询确认批次预报成功；如需核对可运行 wb.py mabang-forecast --check）")
    return 0

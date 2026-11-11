"""matching。"""
import csv


import re
import time
import requests
from sellerops.settings import paths as config
from sellerops.core import utils as common
from sellerops.integrations.mabang.client import get_mabang_cred as mb_client_get_mabang_cred
from sellerops.integrations.mabang.orders import fetch_pending_orders as mb_client_fetch_pending_orders
from sellerops.integrations.mabang.client import REQUEST_INTERVAL as mb_client_REQUEST_INTERVAL
from sellerops.integrations.mabang.client import api_ready as mb_client_api_ready
from sellerops.integrations.mabang.orders import fetch_order_item_ids as mb_client_fetch_order_item_ids
from sellerops.integrations.mabang.orders import search_stock as mb_client_search_stock
from sellerops.integrations.mabang.orders import replace_order_item as mb_client_replace_order_item
from sellerops.integrations.mabang.session import MabangAuthError
from sellerops.persistence.price_book import GROUP_SHIPPING_MARKER

VC_RE = re.compile(r"(BCS-[A-Z]{4}-(?:(?:ozon-card-)?[A-Za-z0-9-]+|[^/*\s]+/\d+))(?:\*\d+)?$")

SKU_RE = re.compile(r"商品编号\*数量:(.+?)\*?\d*$")

TITLE_SKU_RE = re.compile(r"数量:(.*)$")

STATUS_GROUP_SHIPPING = "GROUP_SHIPPING"

def load_local_mapping():
    """返回 (vc→中文名, 中文名→价格表库存SKU栏值)，保留发货群标记供分流。

    2026-10-29：改为**按表头名定位**，不再硬编码列号（旧实现读 `r[1]/r[0]` 与
    `r[2]/r[7]`，一旦用户插列/挪列就会静默读错 —— 轻则全部判 NO_SKU，
    重则把订单商品替换成错误 SKU，导致发错货）。
      - vc→中文名：走 `MappingRepository.load_mapping_state()`（映射总表按表头定位，
        并自动应用 `vc_override.json` 人工纠偏）
      - 中文名→库存SKU：商品价格表按表头定位「产品中文名」+「库存SKU」两列
        （`price_book_columns`，缺列不 raise，只在缺「库存SKU」时告警）
    """
    from openpyxl import load_workbook
    from sellerops.persistence.price_book import price_book_columns
    from sellerops.persistence.mappings import MappingRepository

    state, _ = MappingRepository.load_mapping_state()
    vc2cn = {str(vc).strip(): str(st.get("cn") or "").strip() for vc, st in state.items() if vc}

    cn2sku = {}
    wb = load_workbook(config.PRICE_BOOK_XLSX, read_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        cols = price_book_columns(ws, config.PRICE_BOOK_XLSX, required=("cn",))
        i_cn, i_stock = cols["cn"], cols["stock_sku"]
        if i_stock is None:
            print("[警告] 商品价格表缺少「库存SKU」列：所有订单将判 NO_SKU，请先补该列")
        for r in ws.iter_rows(min_row=2, values_only=True):
            cn = r[i_cn] if i_cn is not None and len(r) > i_cn else None
            sku = r[i_stock] if i_stock is not None and len(r) > i_stock else None
            if cn and sku:
                name = str(cn).strip()
                if cn2sku.get(name) != GROUP_SHIPPING_MARKER:
                    cn2sku[name] = str(sku).strip()
    finally:
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
        if sku == GROUP_SHIPPING_MARKER:
            rec["status"] = STATUS_GROUP_SHIPPING
            rec["note"] = "价格表标记在发货群发货，跳过马帮匹配、预报和交运"
            recs.append(rec)
            continue
        rec["expected_sku"] = sku
        same = (sku.lower() == p["matched_sku"].lower())
        # ★ 用户规则：无论系统是否已匹配（含字符串一致），一律强制更换为 VC 链路查到的 SKU
        rec["status"] = "REPLACE"
        rec["note"] = f"系统匹配={p['matched_sku'] or '(空)'}；字符串{'一致' if same else '不一致'}"
        recs.append(rec)
    return recs

STATUS_REPLACE_FAIL = "REPLACE_FAIL"

REPLACE_RETRY = 1

REPLACE_RETRY_DELAY = 2

def _retryable(e):
    """仅「超时 / 连接失败 / 5xx」可重试；确定性错误（业务码异常、SKU 未找到等）一律不重试。"""
    if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    resp = getattr(e, "response", None)
    return (isinstance(e, requests.exceptions.HTTPError)
            and resp is not None and resp.status_code >= 500)

def apply_replace(cred, recs, warehouse_id):
    """对所有 REPLACE 订单强制执行更换；返回（成功数，失败数）。

    失败时把 `status` 改成 `REPLACE_FAIL`（区别于「计划更换」），并保留中文备注；
    提交前的读取网络异常可重试；更换请求一旦发出，不重放未知结果。
    """
    ok = fail = 0
    for rec in recs:
        if rec["status"] != "REPLACE":
            continue
        err = None
        write_started = False
        for attempt in range(REPLACE_RETRY + 1):
            try:
                ids = mb_client_fetch_order_item_ids(cred, rec["order_id"])
                if len(ids) != 1:
                    raise RuntimeError(f"orderItemId 数量={len(ids)}，需人工处理")
                stock = mb_client_search_stock(cred, rec["expected_sku"], warehouse_id)
                if stock is None:
                    raise RuntimeError(f"马帮未找到库存 SKU {rec['expected_sku']}")
                write_started = True
                mb_client_replace_order_item(cred, ids[0], stock["id"], warehouse_id)
                suffix = "（首次异常，重试成功）" if attempt else ""
                rec["note"] = f"已更换 -> {stock['stockSku']}(stockId={stock['id']}){suffix}"
                ok += 1
                err = None
                break
            except MabangAuthError as exc:
                rec["status"] = STATUS_REPLACE_FAIL
                rec["note"] = f"认证中止: {exc}"
                raise
            except Exception as e:          # noqa: BLE001 —— 逐单业务失败不中断整批
                err = e
                if not write_started and attempt < REPLACE_RETRY and _retryable(e):
                    print(f"  [重试] {rec['order_id']} 更换异常"
                          f"（{type(e).__name__}: {str(e)[:60]}），{REPLACE_RETRY_DELAY}s 后重试 ...")
                    time.sleep(REPLACE_RETRY_DELAY)
                    continue
                break
        if err is not None:
            rec["status"] = STATUS_REPLACE_FAIL
            suffix = "（已重试1次）" if attempt else ""
            rec["note"] = f"更换失败: {str(err)[:80]}{suffix}"
            fail += 1
        time.sleep(mb_client_REQUEST_INTERVAL)
    return ok, fail

CSV_FIELDS = ["状态", "订单ID", "平台单号", "店铺", "付款时间", "VC码", "中文名",
              "系统匹配SKU", "期望SKU", "备注"]

CSV_NAME_TPL = "马帮订单匹配_{ts}.csv"

CSV_NAME_ALIAS = "mabang_order_match_{ts}.csv"

def write_csv(recs):
    """写匹配明细 CSV；返回实际路径，全部失败返回 ""（不抛异常，不阻塞主链路）。"""
    def _w(f):
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in recs:
            w.writerow({"状态": r["status"], "订单ID": r["order_id"],
                        "平台单号": r["platform_order_id"],
                        "店铺": (f"{r['shop_bcs']}({r['shop']})" if r["shop_bcs"] else r["shop"]),
                        "付款时间": r["paid_time"], "VC码": r["vc"],
                        "中文名": r["cn_name"], "系统匹配SKU": r["matched_sku"],
                        "期望SKU": r["expected_sku"], "备注": r["note"]})

    return common.write_csv_preferred(config.LOG_DIR, CSV_NAME_TPL, CSV_NAME_ALIAS,
                                      time.strftime("%Y%m%d_%H%M%S"), _w)

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

    def _w(f):
        w = csv.writer(f)
        w.writerow(["平台单号", "店铺", "中文名", "BCS编号", "说明"])
        for r in rows:
            w.writerow([r["order_id"], r["shop_bcs"] or r["shop"],
                        r["cn_name"], r["vc"], r["note"]])

    path = common.write_csv_preferred(config.LOG_DIR, "缺库存SKU订单_{ts}.csv",
                                      "mabang_no_stock_sku_{ts}.csv",
                                      time.strftime("%Y%m%d_%H%M%S"), _w)
    if path:
        print(f"[报告] 明细: {path}")

def report_replace_fail(recs):
    """REPLACE_FAIL 明细报告（控制台 + CSV）：本轮更换失败、未进预报/交运，需人工处理。

    与 NO_SKU 同待遇：只登记/匹配，不进批次生成/上传/交运。
    """
    rows = [r for r in recs if r.get("status") == STATUS_REPLACE_FAIL]
    if not rows:
        return ""
    print(f"\n[⚠ 需人工处理] 库存SKU 更换失败 {len(rows)} 单"
          "（本轮不进预报/交运；飞书登记照常）：")
    for r in rows:
        print(f"  {r['order_id']} [{r['shop_bcs'] or r['shop']}] "
              f"{r['vc'] or '(无VC)'} {r['cn_name']} | {r['note']}")

    def _w(f):
        w = csv.writer(f)
        w.writerow(["订单ID", "平台单号", "店铺", "中文名", "BCS编号",
                    "期望SKU", "系统匹配SKU", "失败原因"])
        for r in rows:
            w.writerow([r["order_id"], r["platform_order_id"] or "",
                        r["shop_bcs"] or r["shop"], r["cn_name"], r["vc"],
                        r["expected_sku"], r["matched_sku"], r["note"]])

    path = common.write_csv_preferred(config.LOG_DIR, "更换失败订单_{ts}.csv",
                                      "mabang_replace_fail_{ts}.csv",
                                      time.strftime("%Y%m%d_%H%M%S"), _w)
    if path:
        print(f"[报告] 更换失败明细: {path}")
    return path

def run(args, *, continue_on_item_failure: bool = False) -> int:
    common.ensure_utf8_stdout()
    # 保留本轮分流和更换失败的订单 ID，供组合流程复用。
    setattr(args, "replace_fail_order_ids", [])
    args.group_shipping_order_ids = set()
    cred = mb_client_get_mabang_cred()
    warehouse_id = int(cred.get("warehouse_id") or 1457537)
    shop_map = cred["shop_map"]
    print(f"[配置] 目标店铺: {'、'.join(f'{k}→{v}' for k, v in shop_map.items())}")
    scope = f"最近 {args.days} 天" if args.days else "全部新增，不限日期"
    print(f"\n[查询] 待处理订单（{scope}，仅处理配置店铺，tabId=7）...")
    orders = mb_client_fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    if not orders:
        print("[完成] 无待处理订单")
        return 0

    vc2cn, cn2sku = load_local_mapping()
    recs = classify(orders, vc2cn, cn2sku, shop_map)
    args.group_shipping_order_ids = {str(r["order_id"]) for r in recs
                                     if r["status"] == STATUS_GROUP_SHIPPING and r["order_id"]}

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

    n = stat.get("REPLACE", 0)
    if args.apply and n:
        if not mb_client_api_ready(cred):
            print("[错误] 更换订单商品需要马帮 API 凭证，请先运行 mabang-login 更新会话。")
            write_csv(recs)
            return 1
        print(f"\n[执行] 强制更换 {n} 个订单商品（含系统匹配一致者；仓库 {warehouse_id}）...")
        try:
            ok, fail = apply_replace(cred, recs, warehouse_id)
        except MabangAuthError:
            report_replace_fail(recs)
            path = write_csv(recs)
            if path:
                print(f"[汇总] 认证中止前的更换明细: {path}")
            raise
        tail = "（失败单已标 REPLACE_FAIL，本轮不进预报/交运）" if fail else ""
        print(f"  更换成功 {ok} / 失败 {fail}{tail}")
        # 供 mabang-process 步骤②按订单ID排除（此处赋值须早于下方报告/CSV）
        setattr(args, "replace_fail_order_ids",
                [str(r["order_id"]) for r in recs if r["status"] == STATUS_REPLACE_FAIL])
    elif not args.apply:
        print("\n（dry-run 模式未做任何更换；确认无误后加 --apply 执行）")

    report_no_sku(recs)
    report_replace_fail(recs)

    csv_path = write_csv(recs)
    print(f"\n[汇总] 明细: {csv_path or '（未生成，见上方警告）'}")
    # The combined workflow handles failed IDs explicitly and keeps other orders moving.
    return 1 if args.replace_fail_order_ids and not continue_on_item_failure else 0

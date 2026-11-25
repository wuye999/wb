"""matching。"""
import csv
from dataclasses import dataclass, field
import re
import time
import requests
from sellerops.settings import paths as config
from sellerops.core import utils as common
from sellerops.integrations.mabang.client import get_mabang_cred
from sellerops.integrations.mabang.orders import fetch_pending_orders
from sellerops.integrations.mabang.client import REQUEST_INTERVAL
from sellerops.integrations.mabang.client import api_ready
from sellerops.integrations.mabang.orders import fetch_order_item_ids
from sellerops.integrations.mabang.orders import search_stock
from sellerops.integrations.mabang.orders import replace_order_item
from sellerops.integrations.mabang.session import MabangAuthError
from sellerops.persistence.price_book import SHIPPING_MODE_MABANG, SHIPPING_MODE_SKIP, SHIPPING_MODE_WB

VC_RE = re.compile(r"(BCS-[A-Z]{4}-(?:(?:ozon-card-)?[A-Za-z0-9-]+|[^/*\s]+/\d+))(?:\*\d+)?$")


TITLE_SKU_RE = re.compile(r"数量:(.*)$")

STATUS_GROUP_SHIPPING = "GROUP_SHIPPING"
STATUS_SKIP_SHIPPING = "SKIP_SHIPPING"

def load_local_mapping() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """按完整 VC 读取同一商品行的中文名、马帮库存 SKU 和发货方式。"""
    from sellerops.persistence import db
    from sellerops.persistence.mappings import MappingRepository

    MappingRepository.load_price_book(require_price=False, fresh=True)
    entries = db.vc_entries()
    names, stocks, modes = {}, {}, {}
    for vc in entries:
        row = db.resolve_vc(vc, entries)
        if row['status'] == 'mapped':
            names[vc] = row['cn']
            stocks[vc] = row['stock_sku']
            modes[vc] = row['shipping_mode']
    return names, stocks, modes


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

def classify(orders, vc2cn, vc2sku, vc2mode, shop_map):
    """按完整 VC 确认商品，根据价格表的发货方式分流目标店铺订单。"""
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
        mode = vc2mode.get(p["vc"])
        sku = vc2sku.get(p["vc"])
        if mode == SHIPPING_MODE_WB:
            rec["status"] = STATUS_GROUP_SHIPPING
            rec["note"] = "价格表标记WB发货，跳过马帮匹配、预报和交运"
        elif mode == SHIPPING_MODE_SKIP:
            rec["status"] = STATUS_SKIP_SHIPPING
            rec["note"] = "价格表标记不处理，跳过发货"
        elif mode != SHIPPING_MODE_MABANG:
            rec["status"] = "NO_SKU"
            rec["note"] = "价格表发货方式未填或无效，请填 马帮发货/WB发货/不处理"
        elif not sku:
            rec["status"] = "NO_SKU"
            rec["note"] = "发货方式为马帮发货，但缺少马帮库存SKU"
        else:
            rec["expected_sku"] = sku
            same = (sku.lower() == p["matched_sku"].lower())
            # 系统匹配一致时也强制更换为价格表指定的 SKU。
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
                ids = fetch_order_item_ids(cred, rec["order_id"])
                if len(ids) != 1:
                    raise RuntimeError(f"orderItemId 数量={len(ids)}，需人工处理")
                stock = search_stock(cred, rec["expected_sku"], warehouse_id)
                if stock is None:
                    raise RuntimeError(f"马帮未找到库存 SKU {rec['expected_sku']}")
                write_started = True
                replace_order_item(cred, ids[0], stock["id"], warehouse_id)
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
        time.sleep(REQUEST_INTERVAL)
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

    return common.write_csv_preferred(str(config.current.exports_dir), CSV_NAME_TPL, CSV_NAME_ALIAS,
                                      time.strftime("%Y%m%d_%H%M%S"), _w)

def report_no_sku(recs):
    """报告需要修正发货方式或补填马帮库存 SKU 的订单。"""
    rows = [r for r in recs if r.get("status") == "NO_SKU"]
    if not rows:
        return
    print(f"\n[⚠ 需人工处理] 价格表发货配置待补正的订单 {len(rows)} 单"
          "（仅登记/匹配，未进批次生成/上传/交运）：")
    for r in rows:
        print(f"  {r['order_id']} [{r['shop_bcs'] or r['shop']}] "
              f"{r['vc'] or '(无VC)'} {r['cn_name']} | {r['note']}")

    def _w(f):
        w = csv.writer(f)
        w.writerow(["平台单号", "店铺", "中文名", "BCS编号", "说明"])
        for r in rows:
            w.writerow([r["order_id"], r["shop_bcs"] or r["shop"],
                        r["cn_name"], r["vc"], r["note"]])

    path = common.write_csv_preferred(str(config.current.exports_dir), "缺库存SKU订单_{ts}.csv",
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

    path = common.write_csv_preferred(str(config.current.exports_dir), "更换失败订单_{ts}.csv",
                                      "mabang_replace_fail_{ts}.csv",
                                      time.strftime("%Y%m%d_%H%M%S"), _w)
    if path:
        print(f"[报告] 更换失败明细: {path}")
    return path



@dataclass
class MatchResult:
    stopped: bool = False
    failures: dict[str, str] = field(default_factory=dict)
    group_ids: set[str] = field(default_factory=set)
    matched_ids: set[str] = field(default_factory=set)

    @property
    def failed_ids(self) -> set[str]:
        return set(self.failures)


def match_orders(*, days: int, page_size: int, apply: bool) -> MatchResult:
    common.ensure_utf8_stdout()
    # 保留本轮分流和更换失败的订单 ID，供组合流程复用。
    result = MatchResult()
    cred = get_mabang_cred()
    shop_map = cred["shop_map"]
    print(f"[配置] 目标店铺: {'、'.join(f'{k}→{v}' for k, v in shop_map.items())}")
    scope = f"最近 {days} 天" if days else "全部新增，不限日期"
    print(f"\n[查询] 待处理订单（{scope}，仅处理配置店铺，tabId=7）...")
    orders = fetch_pending_orders(cred, days=days, page_size=page_size)
    if not orders:
        print("[完成] 无待处理订单")
        return result

    vc2cn, vc2sku, vc2mode = load_local_mapping()
    recs = classify(orders, vc2cn, vc2sku, vc2mode, shop_map)
    result.group_ids = {str(r["order_id"]) for r in recs
                                     if r["status"] == STATUS_GROUP_SHIPPING and r["order_id"]}

    stat = {}
    for r in recs:
        stat[r["status"]] = stat.get(r["status"], 0) + 1
    target_n = len(recs) - stat.get("SKIP_SHOP", 0)
    print(f"\n[匹配] 共 {len(recs)} 单：目标店铺 {target_n} 单，"
          f"跳过非目标店铺 {stat.get('SKIP_SHOP', 0)} 单；"
          + " / ".join(f"{k}={v}" for k, v in sorted(stat.items())))
    for r in recs:
        if r["status"] not in ("SKIP_SHOP", "REPLACE") or not apply:
            print(f"  [{r['status']}] {r['order_id']} [{r['shop_bcs'] or r['shop']}] "
                  f"{r['vc'] or '(无VC)'} {r['cn_name']} | {r['note']}")

    n = stat.get("REPLACE", 0)
    if apply and n:
        from sellerops.domain.snapshot_values import positive_id
        warehouse_id = positive_id(cred.get("warehouse_id"))
        if warehouse_id is None:
            print("[中止] 请在马帮配置中填写经核实的马帮仓库 ID（mabang.warehouse_id）")
            result.stopped = True
            return result
        if not api_ready(cred):
            print("[错误] 更换订单商品需要马帮 API 凭证，请先运行 sellerops login mabang 更新会话。")
            write_csv(recs)
            result.stopped = True
            return result
        print(f"\n[执行] 强制更换 {n} 个订单商品（含系统匹配一致者；马帮仓库 ID（mabang.warehouse_id）为 {warehouse_id}）...")
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
    elif not apply:
        print("\n（dry-run 模式未做任何更换；确认无误后加 --apply 执行）")

    report_no_sku(recs)
    report_replace_fail(recs)

    csv_path = write_csv(recs)
    print(f"\n[汇总] 明细: {csv_path or '（未生成，见上方警告）'}")
    # The combined workflow handles failed IDs explicitly and keeps other orders moving.
    result.failures = {str(r["order_id"]): r["note"] for r in recs if r["order_id"]
                       and r["status"] not in {"SKIP_SHOP", STATUS_GROUP_SHIPPING, STATUS_SKIP_SHIPPING, "REPLACE"}}
    result.matched_ids = {str(r["order_id"]) for r in recs if r["status"] == "REPLACE" and r["order_id"]}
    return result


def run(args) -> int:
    result = match_orders(days=args.days, page_size=args.page_size, apply=args.apply)
    return int(result.stopped or bool(result.failed_ids))

"""forecast selection。"""
import csv


import re
import time

from sellerops.settings import paths as config
from sellerops.core import utils as common
from sellerops.integrations.mabang.client import get_mabang_cred as mb_client_get_mabang_cred
from sellerops.integrations.mabang.orders import fetch_pending_orders as mb_client_fetch_pending_orders
from sellerops.features.orders.matching import STATUS_GROUP_SHIPPING
from sellerops.features.orders.matching import classify
from sellerops.features.orders.matching import load_local_mapping
from sellerops.features.orders.matching import report_no_sku

FORECAST_CSV_FIELDS = ["类型", "订单ID", "平台单号", "店铺", "批次号", "数量",
                       "生成批次", "上传", "交运", "备注",
                       "物流跟踪号", "批次重量", "上传货代"]

def write_forecast_csv(order_recs, batch_rows):
    def _w(f):
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

    return common.write_csv_preferred(config.LOG_DIR, "马帮预报批次_{ts}.csv",
                                      "mabang_forecast_batch_{ts}.csv",
                                      time.strftime("%Y%m%d_%H%M%S"), _w)

def collect_forecast_orders(args):
    """查待处理订单 → 目标店铺 → VC 链路解析成功(REPLACE)的订单，返回 (recs, cred)"""
    cred = mb_client_get_mabang_cred()
    shop_map = cred["shop_map"]
    print(f"[配置] 目标店铺: {'、'.join(f'{k}→{v}' for k, v in shop_map.items())}")
    scope = f"最近 {args.days} 天" if args.days else "全部新增，不限日期"
    print(f"\n[查询] 待处理订单（{scope}）...")
    orders = mb_client_fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    if not orders:
        print("[完成] 无待处理订单")
        return [], cred
    vc2cn, cn2sku = load_local_mapping()
    recs = classify(orders, vc2cn, cn2sku, shop_map)
    group_shipping_ids = getattr(args, "group_shipping_order_ids", ())
    for rec in recs:
        if rec["status"] != "SKIP_SHOP" and str(rec["order_id"]) in group_shipping_ids:
            rec["status"] = STATUS_GROUP_SHIPPING
    target = [r for r in recs if r["status"] == "REPLACE"]
    other = {}
    for r in recs:
        if r["status"] != "REPLACE":
            other[r["status"]] = other.get(r["status"], 0) + 1
    # 步骤① 更换失败的单：仅当 mabang-process 把失败ID传进来才排除
    # （单独跑 mabang-forecast 时 args 上无此属性 ⇒ 行为与旧版完全一致，不做 matched_sku 自检）
    fail_ids = {str(x) for x in (getattr(args, "replace_fail_order_ids", None) or ())}
    if fail_ids:
        hit = [r for r in target if str(r["order_id"]) in fail_ids]
        if hit:
            target = [r for r in target if str(r["order_id"]) not in fail_ids]
            other["更换失败"] = other.get("更换失败", 0) + len(hit)
    # 已取消订单排除（WB 门户取消单；马帮不同步取消单，防御性过滤）
    from sellerops.integrations.wildberries import client as wb_api
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

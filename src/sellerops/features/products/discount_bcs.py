"""显式 BCS 折扣通道，携带可无损保留的商品级价格。"""
import csv
import time
from datetime import datetime
from pathlib import Path

from sellerops.domain.snapshot_values import bcs_keep_price, positive_id, quantity
from sellerops.features.products.execution import submit_bcs_prices
from sellerops.features.products.plans import report_plan_skips
from sellerops.features.products.results import ProductResult
from sellerops.integrations.bcs.client import client as bcs
from sellerops.persistence import db
from sellerops.settings import paths as config

SHOP_SLEEP = 0.6
FILTER_DEF = "BASE"


def collect(shop_id: int, threshold: int, limit: int, target: int, *, entries: dict | None = None) -> dict:
    entries = entries if entries is not None else db.vc_entries()
    rows = bcs.fetch_shop_products(shop_id, FILTER_DEF)
    plan = {"shopId": shop_id, "items": [], "blocked_items": [], "skipped_items": []}
    for row in rows:
        value = quantity(row.get("discount"))
        discount = value if value is not None and value <= 100 else None
        nm_id = positive_id(row.get("nmId"))
        vc = row.get("vendorCode")
        item = {"nmID": nm_id, "vc": vc or "", "title": row.get("title"),
                "old_discount": discount, "discount": target}
        if vc and db.resolve_vc(vc, entries)['status'] == 'excluded':
            plan['skipped_items'].append({**item, 'reason': '\u8be5\u5546\u54c1\u5df2\u6807\u8bb0\u4e3a\u975e\u8d27\u76d8'})
            continue
        if discount is not None and (discount <= threshold or discount == target):
            plan["skipped_items"].append({**item, "reason": "折扣不满足阈值或已等于目标"})
            continue
        if not nm_id or not vc:
            reason = "缺少本店商品 ID 或完整 VC"
        elif threshold >= 0 and discount is None:
            reason = "缺少判断阈值需要的原折扣"
        else:
            price = bcs_keep_price(row)
            reason = "无法无损保持全部规格的单一原价" if price is None else ""
        if reason:
            plan["blocked_items"].append({**item, "reason": reason})
            continue
        plan["items"].append({**item, "price": price})
        if limit and len(plan["items"]) >= limit:
            break
    return plan


def _write_csv(result: ProductResult, names: dict[int, str]) -> None:
    from sellerops.persistence.product_results import STATUS_LABELS

    rows = []
    for sid in result.shops:
        label = f"{sid}({names[sid]})" if sid in names else str(sid)
        for receipt in result.receipts[sid]:
            item = receipt.item
            rows.append([label, item.get("nmID"), item.get("vc"), item.get("title"),
                         item.get("old_discount"), result.targets["discount"], item.get("price"),
                         f"{STATUS_LABELS[receipt.status]} {receipt.detail}".strip()])
    if not rows:
        return
    output = Path(config.current.log_dir) / f"折扣修改_bcs_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["店铺", "nmID", "vendorCode", "标题", "原折扣", "目标折扣", "价格", "结果"])
        writer.writerows(rows)
    result.artifacts["discount_csv"] = output


def run(args) -> int:
    return ProductResult("discount", "bcs", args).run(_run, args)


def _run(args, result: ProductResult) -> None:
    plans = getattr(args, 'frozen', None)
    if plans is None:
        shops = {int(shop["id"]): shop["name"] for shop in bcs.fetch_shop_list()}
        if not result.shops:
            result.shops = list(shops)
        if not result.shops:
            raise ValueError("BCS 未返回可处理的店铺")
        entries = db.vc_entries()
        plans = []
        for sid in result.shops:
            try:
                if sid not in shops:
                    raise ValueError("BCS 店铺列表中无此店")
                plan = collect(sid, args.threshold, args.limit, args.target, entries=entries)
            except Exception as error:
                reason = str(error) if isinstance(error, ValueError) else f"本店折扣数据读取失败（{type(error).__name__}）"
                plan = {"shopId": sid, "items": [], "blocked_items": [], "skipped_items": [], "range_error": reason}
            plans.append(plan)
        args.prepared = plans
    else:
        shops = {}
    report_plan_skips(plans, "BCS 改折扣")
    result.add_plans(plans)
    for plan in plans:
        if not plan["items"]:
            continue
        sid = plan["shopId"]
        if result.apply:
            submit_bcs_prices(sid, plan["items"], result)
            time.sleep(SHOP_SLEEP)
        else:
            for item in plan["items"]:
                print(f"  店{sid} {item['vc']}：折扣 {item['old_discount']} → {args.target}%")
    _write_csv(result, shops)

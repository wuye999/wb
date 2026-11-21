"""使用显式 BCS 通道批量调整商品折扣，携带该店价格。"""
import csv
import os
import time

from sellerops.integrations.bcs.client import client as bcs
from sellerops.settings import paths as config
from sellerops.core import utils as common
from sellerops.domain.snapshot_values import positive_id, quantity
from sellerops.persistence.price_book import to_number
from math import isfinite

CHUNK = 300
BATCH_SLEEP = 0.15
SHOP_SLEEP = 0.6
FILTER_DEF = "BASE"


def collect(shop_id, threshold, limit, skip_equal_to=None):
    """拉取单店在架商品，过滤 discount>threshold（且跳过已等于目标折扣的），
    返回待改清单。"""
    rows = bcs.fetch_shop_products(shop_id, FILTER_DEF)
    items, blocked, skip_other = [], [], 0
    for row in rows:
        discount = quantity(row.get("discount"))
        if discount is not None and discount > 100:
            discount = None
        if discount is not None and (discount <= threshold or discount == skip_equal_to):
            skip_other += 1
            continue
        nm_id = positive_id(row.get("nmId"))
        vc = row.get("vendorCode")
        reason = ""
        if not nm_id or not vc:
            reason = "缺少本店商品 ID 或完整 VC"
        elif threshold >= 0 and discount is None:
            reason = "缺少判断阈值需要的原折扣"
        sizes = row.get("sizeList") or []
        prices = {to_number(size.get("price")) if isinstance(size, dict) else None for size in sizes}
        if len(prices) != 1 or any(p is None or not isfinite(p) or p <= 0 for p in prices):
            reason = reason or "无法确定保持各规格原价的单一价格"
        if reason:
            blocked.append({"nmID": nm_id, "vc": vc, "reason": reason})
            continue
        price = prices.pop()
        items.append({
            "nmID": nm_id,
            "vc": vc,
            "title": row.get("title"),
            "old_d": discount,
            "price": price,
        })
        if limit and len(items) >= limit:
            break
    return items, {"blocked": blocked, "skip_other": skip_other}


def apply(shop_id, items, target):
    dl = [{"nmID": it["nmID"], "price": it["price"],
           "discount": target, "clubDiscount": None} for it in items]
    results = {}
    for i in range(0, len(dl), CHUNK):
        chunk = dl[i:i + CHUNK]
        try:
            r = bcs.batch_set_prices(shop_id, chunk)
            if r.get("code") == 200:
                for c in chunk:
                    results[c["nmID"]] = "成功"
            else:
                for c in chunk:
                    results[c["nmID"]] = f"失败(code={r.get('code')},msg={r.get('msg')})"
        except Exception as e:
            for c in chunk:
                results[c["nmID"]] = f"错误:{str(e)[:60]}"
        if i + CHUNK < len(dl):
            time.sleep(BATCH_SLEEP)
    time.sleep(SHOP_SLEEP)
    return results


def write_csv(rows):
    os.makedirs(config.current.log_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.current.log_dir, f"折扣修改_bcs_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["店铺", "nmID", "vendorCode", "标题", "原折扣", "目标折扣", "价格", "结果"])
        w.writeheader()
        w.writerows(rows)
    return path


def run(args):
    common.ensure_utf8_stdout()
    shops = bcs.fetch_shop_list()
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if s["id"] in want]
        if missing := want - {s["id"] for s in shops}:
            print(f"[错误] 指定店铺 {sorted(missing)} 未找到")
            return 1
    print(f"店铺 {len(shops)} 个: {[(s['id'], s['name']) for s in shops]}")
    if not shops:
        print("[错误] 没有可处理的店铺")
        return 1


    print(f"\n[收集] 阈值 >{args.threshold}%，逐店拉取在架商品...")
    plan, stats = {}, {}
    collection_errors = submission_failures = 0
    rows = []
    for s in shops:
        sid = s["id"]
        try:
            items, st = collect(sid, args.threshold, args.limit,
                                skip_equal_to=args.target)
            plan[sid] = items
            stats[sid] = st
            blocked = st["blocked"]
            collection_errors += bool(blocked)
            rows.extend({"店铺": f"{sid}({s['name']})", "nmID": it["nmID"], "vendorCode": it["vc"],
                         "结果": f"阻止：{it['reason']}"} for it in blocked)
            skip_eq = st.get("skip_other", 0)
            suffix = f"（必要字段不足 {len(blocked)}、已等于目标 {skip_eq}）"
            print(f"  店{sid} ({s['name']}): 在架待改 {len(items)} 条{suffix}")
        except Exception as e:
            collection_errors += 1
            stats[sid] = {"error": str(e)}
            print(f"  [警告] 店{sid} 收集失败: {e}（降级跳过）")
        time.sleep(0.5)

    total = sum(len(v) for v in plan.values())
    if total == 0:
        if collection_errors:
            if rows:
                print(f"[完整跳过报告] {write_csv(rows)}")
            print(f"\n[失败] {collection_errors} 个店铺收集失败，不能判定无需修改")
            return 1
        print(f"\n[完成] 所有店铺均无 discount > {args.threshold}% 的商品，无需修改")
        return 0

    print(f"\n{'[dry-run] 待改清单（未提交）' if not args.apply else '[执行] 开始提交改折扣...'}")
    for s in shops:
        sid = s["id"]
        for it in plan.get(sid, []):
            rows.append({"店铺": f"{sid}({s['name']})", "nmID": it["nmID"], "vendorCode": it["vc"],
                         "标题": it["title"], "原折扣": it["old_d"], "目标折扣": args.target,
                         "价格": it["price"], "结果": "DRY" if not args.apply else "待提交"})

    if args.apply:
        for s in shops:
            sid = s["id"]
            items = plan.get(sid, [])
            if not items:
                continue
            try:
                results = apply(sid, items, args.target)
                ok_n = sum(1 for it in items if results.get(it["nmID"]) == "成功")
                fail_n = len(items) - ok_n
                submission_failures += fail_n
                for row in rows:
                    if row["店铺"].startswith(f"{sid}(") and row["结果"] == "待提交":
                        row["结果"] = results.get(row["nmID"], "未知")
                print(f"  店{sid}: 成功 {ok_n} 条，失败/错误 {fail_n} 条")
            except Exception as e:
                submission_failures += len(items)
                print(f"  [警告] 店{sid} 提交失败: {e}")
                for row in rows:
                    if row["店铺"].startswith(f"{sid}(") and row["结果"] == "待提交":
                        row["结果"] = f"错误:{str(e)[:40]}"


    csv_path = write_csv(rows)
    print(f"\n[汇总] 共 {total} 条待改 | 日志: {csv_path}")
    if not args.apply:
        print("（dry-run 模式未提交任何修改；确认无误后加 --apply 执行）")
        return 1 if collection_errors else 0
    common.print_write_hint()
    return 1 if collection_errors or submission_failures else 0

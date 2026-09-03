# -*- coding: utf-8 -*-
"""
wb_ops 折扣改价（原 wb_bcs_discount.py）

把所有店铺（BCS 自动获取）中「折扣 > 阈值」的在架商品改为目标折扣（默认 >50% → 50%）。
只改 discount，price 保持当前值、clubDiscount 不动。默认 dry-run。
"""
import csv
import os
import time

from . import bcs
from . import config
from . import common

CHUNK = 300
BATCH_SLEEP = 0.15
SHOP_SLEEP = 0.6
FILTER_DEF = "BASE"


def collect(shop_id, threshold, limit):
    """拉取单店在架商品，过滤 discount>threshold，返回待改清单。"""
    rows = bcs.fetch_shop_products(shop_id, FILTER_DEF)
    items, skip_no_price, skip_other = [], 0, 0
    for r in rows:
        d = common.to_int(r.get("discount"))
        if d <= threshold:
            continue
        sl = r.get("sizeList") or []
        price = None
        for sz in sl:
            p = sz.get("price")
            if p not in (None, 0, "", 0.0):
                price = p
                break
        if price is None:
            skip_no_price += 1
            continue
        items.append({
            "nmID": r.get("nmId"),
            "vc": r.get("vendorCode"),
            "title": r.get("title"),
            "old_d": d,
            "price": price,
        })
        if limit and len(items) >= limit:
            break
    return items, {"skip_no_price": skip_no_price, "skip_other": skip_other}


def apply(shop_id, items, target):
    dl = [{"nmID": it["nmID"], "price": it["price"],
           "discount": target, "clubDiscount": None} for it in items]
    results = {}
    url = bcs.base_url() + "/shopKeeper/price/batch"
    for i in range(0, len(dl), CHUNK):
        chunk = dl[i:i + CHUNK]
        # ★ 2026-09-03 BCS 接口变更：shopId 须在每个 dataList 条目内（同 dimension/batch）
        body = {"dataList": [{**c, "shopId": shop_id} for c in chunk]}
        try:
            r = bcs.http_post_json(url, body)
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
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"折扣修改_{ts}.csv")
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
        if not shops:
            print(f"[错误] 指定店铺 {args.shops} 未找到")
            return 1
    print(f"店铺 {len(shops)} 个: {[(s['id'], s['name']) for s in shops]}")
    if not shops:
        print("[错误] 没有可处理的店铺")
        return 1

    if args.sync:
        print("\n[同步] 刷新 BCS 缓存（约 50s）...")
        bcs.sync_shops_parallel([s["id"] for s in shops])

    print(f"\n[收集] 阈值 >{args.threshold}%，逐店拉取在架商品...")
    plan, stats = {}, {}
    for s in shops:
        sid = s["id"]
        try:
            items, st = collect(sid, args.threshold, args.limit)
            plan[sid] = items
            stats[sid] = st
            no_price = st["skip_no_price"]
            suffix = f"（跳过无价格 {no_price}）" if no_price else ""
            print(f"  店{sid} ({s['name']}): 在架待改 {len(items)} 条{suffix}")
        except Exception as e:
            stats[sid] = {"error": str(e)}
            print(f"  [警告] 店{sid} 收集失败: {e}（降级跳过）")
        time.sleep(0.5)

    total = sum(len(v) for v in plan.values())
    if total == 0:
        print(f"\n[完成] 所有店铺均无 discount > {args.threshold}% 的商品，无需修改")
        return 0

    rows = []
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
                ok_n = sum(1 for v in results.values() if v == "成功")
                fail_n = len(results) - ok_n
                for row in rows:
                    if row["店铺"].startswith(f"{sid}("):
                        row["结果"] = results.get(row["nmID"], "未知")
                print(f"  店{sid}: 成功 {ok_n} 条，失败/错误 {fail_n} 条")
            except Exception as e:
                print(f"  [警告] 店{sid} 提交失败: {e}")
                for row in rows:
                    if row["店铺"].startswith(f"{sid}("):
                        row["结果"] = f"错误:{str(e)[:40]}"

        if args.sync:
            print("\n[复核] 同步 BCS 缓存回读验证（约 50s）...")
            bcs.sync_shops_parallel([s["id"] for s in shops])

    csv_path = write_csv(rows)
    print(f"\n[汇总] 共 {total} 条待改 | 日志: {csv_path}")
    if not args.apply:
        print("（dry-run 模式未提交任何修改；确认无误后加 --apply 执行）")
        return 0
    if not args.sync:
        from . import mapping_sync
        mapping_sync.print_write_hint()
    return 0

# -*- coding: utf-8 -*-
"""
wb_ops 促销活动自动报名（原 wb_promo_apply.py）

逐店（cookie 会话）查询可参加的促销活动并全量报名（applyAll:true）。
流程：timeline?filter=AVAILABLE → 筛选可参加 → detail 取 periodID → apply 报名（幂等跳过已存在）。
"""
import csv
import datetime
import os
import time

from . import config
from . import credentials
from . import common
from . import wb_api
API_BASE = "https://discounts-prices.wildberries.ru/ns/calendar-api/dp-calendar"
URL_TIMELINE = API_BASE + "/web/api/v3/promotions/timeline"
URL_DETAIL = API_BASE + "/web/api/v3/promotions/detail"
URL_APPLY = API_BASE + "/suppliers/api/v2/products/apply"


def fetch_available_promotions(session, start_dt, end_dt):
    url = f"{URL_TIMELINE}?endDate={end_dt}&filter=AVAILABLE&startDate={start_dt}"
    d = wb_api.request(session, "GET", url)
    return (d.get("data") or {}).get("promotions", [])


def get_period_id(session, promo_id):
    d = wb_api.request(session, "GET", f"{URL_DETAIL}?promoID={promo_id}")
    data = d.get("data") or {}
    return data.get("periodID"), data


def apply_promotion(session, period_id):
    d = wb_api.request(session, "POST", URL_APPLY,
                       json={"applyAll": True, "periodID": period_id, "startNow": True})
    data = d.get("data") or {}
    return True, data.get("alreadyExists", False), data


def is_joinable(promo):
    part = promo.get("participation") or {}
    status = part.get("status", "")
    available = (part.get("counts") or {}).get("available", 0)
    return status != "PARTICIPATING" and int(available or 0) > 0


def process_shop(shop, root_version, args, rows):
    st = {"joinable": 0, "applied": 0, "skipped": 0, "failed": 0}
    s = wb_api.make_session(shop, root_version)
    name = shop.get("shopName", shop.get("shopId"))
    print(f"\n=== 店铺 {name} (id={shop['shopId']}) ===")

    now = datetime.datetime.now(datetime.timezone.utc)
    start_dt = (now - datetime.timedelta(days=args.days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_dt = (now + datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    promos = fetch_available_promotions(s, start_dt, end_dt)
    print(f"  [活动列表] 返回 {len(promos)} 个活动")
    if not promos:
        return st

    for p in promos:
        pid = p.get("promoID")
        part = p.get("participation") or {}
        counts = part.get("counts") or {}
        avail = counts.get("available", 0)
        tag = part.get("status", "?")
        if is_joinable(p):
            st["joinable"] += 1
        print(f"    - promoID={pid} 「{p.get('name')}」 {p.get('startDate','')[:10]}~{p.get('endDate','')[:10]}"
              f" status={tag} eligible={counts.get('eligible')} available={avail}")

    for p in promos:
        if not is_joinable(p):
            continue
        pid = p.get("promoID")
        rows.append({"店铺": name, "shopId": shop["shopId"], "活动名": p.get("name"),
                     "promoID": pid, "periodID": "", "available": (p.get("participation") or {}).get("counts", {}).get("available"),
                     "结果": "DRY" if not args.apply else "待提交"})
        period_id, detail = get_period_id(s, pid)
        rows[-1]["periodID"] = period_id
        if not args.apply:
            continue
        if not period_id:
            rows[-1]["结果"] = "失败:无periodID"
            st["failed"] += 1
            continue
        try:
            ok, already, data = apply_promotion(s, period_id)
            if already:
                rows[-1]["结果"] = "已存在(跳过)"
                st["skipped"] += 1
            else:
                rows[-1]["结果"] = f"成功(task={data.get('id')})"
                st["applied"] += 1
        except Exception as e:
            rows[-1]["结果"] = f"失败:{str(e)[:50]}"
            st["failed"] += 1
        print(f"    >> promoID={pid} periodID={period_id} -> {rows[-1]['结果']}")
        time.sleep(args.sleep)
    return st


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    shops = cred.wb_shops()
    if not shops:
        print("[错误] credentials.json 中没有已填 cookie 的店铺（请编辑 data/credentials.json 的 wb.shops）")
        return 1
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if s["shopId"] in want]
        if not shops:
            print(f"[错误] 指定店铺 {args.shops} 未在凭证中找到已填 cookie 的店铺")
            return 1
    root_version = cred.root_version
    shop_names = [f"{s['shopName']}({s['shopId']})" for s in shops]
    print(f"店铺 {len(shops)} 个: {shop_names}{'' if args.apply else '  [dry-run]'}")

    rows = []
    totals = {"joinable": 0, "applied": 0, "skipped": 0, "failed": 0}
    for shop in shops:
        try:
            st = process_shop(shop, root_version, args, rows)
            for k in totals:
                totals[k] += st[k]
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {shop['shopName']}: {e}（该店中止，继续下一店）")
        except Exception as e:
            print(f"  [警告] 店铺 {shop['shopName']} 处理失败: {e}")
        time.sleep(0.5)

    print(f"\n[汇总] 可报名 {totals['joinable']} | "
          f"{'成功 ' + str(totals['applied']) if args.apply else '预览 ' + str(len(rows))}"
          f" | 已存在跳过 {totals['skipped']} | 失败 {totals['failed']}")
    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"报名结果_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "shopId", "活动名", "promoID", "periodID", "available", "结果"])
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")
    if not args.apply:
        print("（dry-run 未提交任何报名；确认无误后加 --apply 执行）")
    return 0

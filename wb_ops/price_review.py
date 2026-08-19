# -*- coding: utf-8 -*-
"""
wb_ops 价格审核「应用新价格」（原「获取价格审核列表、审核价格」抓包）

改价降价 30-49.9% 会进入 WB 价格审查（隔离区 quarantine），需调本模块「应用新价格」才能生效。
流程：GET quarantine/goods 拉待审列表（limit/offset 翻页）→ POST 同路径提交审核（body {"data":[id,...]}）。
鉴权：WB cookie 会话三件套（wb_api.py）。
"""
import csv
import os
import time

from . import config
from . import credentials
from . import common
from . import wb_api

QUARANTINE = "https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/quarantine/goods"
PAGE_SIZE = 100


def fetch_quarantine(session, limit=PAGE_SIZE, offset=0):
    """GET 待审商品列表（单页）。返回 items 列表。"""
    d = wb_api.request(session, "GET", f"{QUARANTINE}?limit={limit}&offset={offset}")
    return (d.get("data") or {}).get("quarantineGoods") or []


def fetch_all_quarantine(session, limit=PAGE_SIZE):
    """翻页拉全部待审商品，直到取完（列表默认只加载前 50 个，可能更多，需翻页）。"""
    all_items = []
    offset = 0
    while True:
        items = fetch_quarantine(session, limit, offset)
        if not items:
            break
        all_items.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return all_items


def apply_prices(session, ids):
    """POST 审核：body {"data": ids}。返回响应 dict。"""
    return wb_api.request(session, "POST", QUARANTINE, json={"data": ids})


def process_shop(shop, root_version, args, rows):
    st = {"total": 0, "applied": 0, "failed": 0}
    s = wb_api.make_session(shop, root_version)
    name = shop.get("shopName", shop.get("shopId"))
    print(f"\n=== 店铺 {name} (id={shop['shopId']}) ===")
    items = fetch_all_quarantine(s)
    st["total"] = len(items)
    print(f"  [待审列表] {len(items)} 个商品待审核")
    if not items:
        return st
    if args.limit > 0:
        items = items[:args.limit]
        print(f"  [截断] 按 --limit 仅处理前 {len(items)} 个")

    for it in items:
        rows.append({
            "店铺": name, "shopId": shop["shopId"],
            "id": it.get("id"), "nmID": it.get("nmID"),
            "vendorCode": it.get("vendorCode"), "title": it.get("title"),
            "oldPrice": it.get("oldPrice"), "newPrice": it.get("newPrice"),
            "priceDiffPercent": it.get("priceDiffPercent"),
            "结果": "DRY" if not args.apply else "待提交",
        })
        print(f"    id={it.get('id')} nmID={it.get('nmID')} {it.get('title')} "
              f"{it.get('oldPrice')}→{it.get('newPrice')} ({it.get('priceDiffPercent')}%)")

    if args.apply:
        ids = [it.get("id") for it in items if it.get("id") is not None]
        if not ids:
            return st
        try:
            d = apply_prices(s, ids)
            ok = not d.get("error")
            if ok:
                st["applied"] = len(ids)
                for row in rows[-len(items):]:
                    row["结果"] = "已应用新价格"
            else:
                st["failed"] = len(ids)
                for row in rows[-len(items):]:
                    row["结果"] = f"失败:{d.get('errorText') or 'error'}"
            print(f"    >> 审核提交 {'成功' if ok else '失败'} {len(ids)} 个"
                  f"{'' if ok else ': ' + str(d.get('errorText'))}")
        except Exception as e:
            st["failed"] = len(ids)
            for row in rows[-len(items):]:
                row["结果"] = f"失败:{str(e)[:50]}"
            print(f"    >> 审核提交异常: {e}")
    return st


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    shops = cred.wb_shops()
    if not shops:
        print("[错误] credentials.json 中没有已填 cookie 的店铺")
        return 1
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if s["shopId"] in want]
        if not shops:
            print(f"[错误] 指定店铺 {args.shops} 未在凭证中找到")
            return 1
    root_version = cred.root_version
    shop_names = [f"{s['shopName']}({s['shopId']})" for s in shops]
    print(f"店铺 {len(shops)} 个: {shop_names}{'' if args.apply else '  [dry-run]'}")

    rows = []
    totals = {"total": 0, "applied": 0, "failed": 0}
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

    print(f"\n[汇总] 待审 {totals['total']} | "
          f"{'已应用新价格 ' + str(totals['applied']) if args.apply else '预览 ' + str(len(rows))}"
          f" | 失败 {totals['failed']}")
    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"价格审核_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "shopId", "id", "nmID", "vendorCode",
                                              "title", "oldPrice", "newPrice", "priceDiffPercent", "结果"])
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")
    if not args.apply:
        print("（dry-run 未提交任何审核；确认清单后加 --apply 执行）")
    return 0

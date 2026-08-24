# -*- coding: utf-8 -*-
"""
wb_ops 折扣快速改价（WB 原生，discount-scan）

模式1：「将高于阈值折扣 → 目标折扣」（默认 >50% → 50%）快速改价。
全程走 WB 原生接口，不触发 BCS 同步，大幅提速（每店通常个位数商品）：
- 列表：discounts-prices .../list/goods/filter（sort=discount&sortOrder=0 从高到低，offset 分页）找 >threshold
- 写：discounts-prices .../nm/upload/task（body {"data":{"nmID":..,"discount":target,"currencyIsoCode":"CNY"}}）逐商品改
- 回验：同一列表接口复查是否仍 >threshold
同一 vc 在不同店铺折扣不同 → 按店铺 cookie 会话逐店逐商品单独处理。
模式2「所有商品→50%」走原 discount.py（BCS 全量同步，慢），见 docs。
"""
import csv
import os
import time

from . import common
from . import config
from . import credentials
from . import wb_api

DISC_LIST = ("https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers"
             "/api/v1/list/goods/filter")
DISC_UPLOAD = ("https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers"
               "/api/v1/nm/upload/task?checkChange=true")
PAGE = 100        # 列表每页条数（offset 分页）
MAX_PAGES = 200   # 分页上限保护
SCAN_SLEEP = 0.3  # 改折扣逐条间隔
VERIFY_WAIT = 4   # 改后回验前等待秒（WB 近乎即时，缓冲异步）
CURRENCY = "CNY"


def _list_body(offset):
    return {"limit": PAGE, "offset": offset, "facets": [],
            "filterWithoutPrice": False, "filterWithLeftovers": False,
            "filterWithoutCompetitivePrice": False, "sort": "discount", "sortOrder": 0}


def fetch_discount_items(session, threshold):
    """WB 列表（从高到低）→ 收集 discount>threshold 的商品。返回 [{nmID,vc,title,old_d}]。
    从高到低排序：某页首条 <=threshold 即不再有高折扣，提前停止。"""
    items, offset, pages = [], 0, 0
    while pages < MAX_PAGES:
        d = wb_api.request(session, "POST", DISC_LIST, json=_list_body(offset))
        goods = (d.get("data") or {}).get("listGoods") or []
        pages += 1
        if not goods:
            break
        first = common.to_int(goods[0].get("discount"))
        if first is not None and first <= threshold:
            break  # 从高到低，首条已 <= 阈值，后续更小
        for g in goods:
            disc = common.to_int(g.get("discount"))
            if disc is None or disc <= threshold:
                continue
            items.append({"nmID": g.get("nmID"), "vc": g.get("vendorCode"),
                          "title": g.get("title") or "", "old_d": disc})
        if len(goods) < PAGE:
            break
        offset += PAGE
        time.sleep(0.2)
    return items


def change_discount(session, nm_id, target):
    """WB 原生改折扣（一次一 nmID，只改 discount）。返回 (ok, reason)。"""
    body = {"data": {"nmID": nm_id, "discount": target, "currencyIsoCode": CURRENCY}}
    try:
        d = wb_api.request(session, "POST", DISC_UPLOAD, json=body)
        ok = (not d.get("error")) and (d.get("data") or {}).get("id") is not None
        return (ok, "" if ok else (d.get("errorText") or "error"))
    except Exception as e:
        return False, str(e)[:80]


def verify_high(session, threshold):
    """改后回验：仍 >threshold 的 nmID 列表。"""
    return [it["nmID"] for it in fetch_discount_items(session, threshold)]


def write_csv(rows):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"折扣快速改_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["店铺", "shopId", "nmID", "vendorCode", "标题",
                                          "原折扣", "目标折扣", "结果"])
        w.writeheader()
        w.writerows(rows)
    return path


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
    threshold, target = args.threshold, args.target
    pair_names = [f"{s['shopName']}({s['shopId']})" for s in shops]
    print(f"店铺 {len(shops)} 个: {pair_names}"
          f" | 目标: >{threshold}% → {target}%{'' if args.apply else '  [dry-run]'}")

    rows, total = [], 0
    for shop in shops:
        name = shop.get("shopName", shop["shopId"])
        sid = shop["shopId"]
        print(f"\n=== 店铺 {name} ({sid}) ===")
        try:
            s = wb_api.make_session(shop, root_version)
            items = fetch_discount_items(s, threshold)
        except common.CookieExpiredError as e:
            print(f"  [警告] {e}（跳过本店）")
            continue
        except Exception as e:
            print(f"  [警告] 列表获取失败: {e}（跳过本店）")
            continue
        if args.limit > 0:
            items = items[: args.limit]
        if not items:
            print(f"  无 discount > {threshold}% 的商品")
            continue
        total += len(items)
        for it in items:
            rows.append({"店铺": name, "shopId": sid, "nmID": it["nmID"], "vendorCode": it["vc"],
                         "标题": it["title"], "原折扣": it["old_d"], "目标折扣": target,
                         "结果": "DRY" if not args.apply else "待提交"})
            print(f"    nmID={it['nmID']} vc={it['vc']} {it['title']}  {it['old_d']}% -> {target}%")

        if args.apply:
            print(f"  [提交] 逐条改折扣...")
            for it in items:
                ok, reason = change_discount(s, it["nmID"], target)
                for row in rows[-len(items):]:
                    if row["nmID"] == it["nmID"]:
                        row["结果"] = "成功" if ok else f"失败:{reason}"
                print(f"    nmID={it['nmID']} {'成功' if ok else '失败：' + reason}")
                time.sleep(SCAN_SLEEP)
            time.sleep(VERIFY_WAIT)
            remain = verify_high(s, threshold)
            print(f"  [回验] 仍 >{threshold}% 剩余 {len(remain)} 个: {remain[:10]}")
        time.sleep(0.5)

    csv_path = write_csv(rows)
    print(f"\n[汇总] 共 {total} 条待改 | 日志: {csv_path}")
    if not args.apply:
        print("（dry-run 未提交任何修改；确认清单后加 --apply 执行）")
    return 0
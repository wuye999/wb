# -*- coding: utf-8 -*-
"""
wb_ops 折扣快速改价（discount-scan）

模式1：「将高于阈值折扣 → 目标折扣」（默认 >50% → 50%）快速改价，采用**混合引擎**：
- 列表：WB discounts-prices .../list/goods/filter（sort=discount&sortOrder=0 从高到低，offset 分页）找 >threshold（实时、唯一数据来源，不触发 BCS 同步）
- 写（提速）：能在本地快照中匹配到 nmID 且可取到当前价 → BCS shopKeeper/price/batch 批量改（一次 ≤300 条）
- 回退（准确）：本地快照/映射表中不存在或无价格的商品 → WB nm/upload/task 单条改，并提示已改用 WB 接口
- 回验：同一 WB 列表接口复查是否仍 >threshold
同一 vc 在不同店铺折扣不同 → 按店铺 cookie 会话逐店处理。
模式2「所有商品→50%」走原 discount.py（BCS 全量同步，慢），见 docs。
"""
import csv
import os
import time

from . import bcs
from . import common
from . import config
from . import credentials
from . import products
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


def _wb_price(raw):
    """WB 列表里的价格字段可能是数值/字符串，转 float；非价格则 None。"""
    try:
        if raw in (None, "", []):
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _price_of_row(row):
    """从 BCS 快照行取首个非空价格（sizeList[].price）。"""
    for sz in row.get("sizeList") or []:
        p = sz.get("price")
        if p not in (None, 0, "", 0.0):
            return p
    return None


def fetch_discount_items(session, threshold):
    """WB 列表（从高到低）→ 收集 discount>threshold 的商品。返回 [{nmID,vc,title,old_d,price}]。
    从高到低排序：某页首条 <=threshold 即不再有高折扣，提前停止。
    price 来自 WB 列表（若返回），否则 None（由调用方回退到本地快照）。"""
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
                          "title": g.get("title") or "", "old_d": disc,
                          "price": _wb_price(g.get("price"))})
        if len(goods) < PAGE:
            break
        offset += PAGE
        time.sleep(0.2)
    return items


def bcs_batch_discount(shop_id, items, target):
    """BCS 批量改折扣（shopKeeper/price/batch，一次 ≤300 条）。
    items: [{nmID, price, ...}]，需携带当前价（BCS 接口要求 price 一起提交，否则可能误改价格）。
    返回 {nmID: 结果文本}。"""
    CHUNK = 300
    BATCH_SLEEP = 0.15
    SHOP_SLEEP = 0.6
    dl = [{"nmID": it["nmID"], "price": it.get("_price"),
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
                msg = f"code={r.get('code')},msg={r.get('msg')}"
                for c in chunk:
                    results[c["nmID"]] = f"失败({msg})"
        except Exception as e:
            for c in chunk:
                results[c["nmID"]] = f"错误:{str(e)[:60]}"
        if i + CHUNK < len(dl):
            time.sleep(BATCH_SLEEP)
    time.sleep(SHOP_SLEEP)
    return results


def load_snapshot_prices():
    """本地快照 → 按店铺名索引 {shopName: {"sid": BCS店id, "prices": {nmID: price|None}}}。
    price=None 表示该 nm 在快照但无价（BCS 批量仍无法用于它）。"""
    shops_data, shops_meta = products.load_all_shops()
    by_sid = {}
    for sid, rows in shops_data.items():
        d = {}
        for r in rows:
            nm = r.get("nmId")
            if nm is None:
                continue
            d[nm] = _price_of_row(r)
        by_sid[sid] = d
    out = {}
    for m in shops_meta:
        d = by_sid.get(m.get("id"))
        if d is not None:
            out[m.get("name")] = {"sid": m.get("id"), "prices": d}
    return out


def load_mapping_vcs():
    """映射表中已归属 vc 集合（仅用于回退提示措辞；失败视为无映射信息）。"""
    try:
        from . import mapping
        state, _ = mapping.load_mapping_state()
        return set(state.keys())
    except Exception:
        return set()


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
                                          "原折扣", "目标折扣", "方式", "原因", "结果"])
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

    # 混合引擎：本地快照（nmID→价格）判能否走 BCS 批量；映射表 vc 集合仅供回退提示措辞
    snapshots = load_snapshot_prices()
    mapping_vcs = load_mapping_vcs()

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

        # 分类：快照可取到价 → BCS 批量；否则 → WB 单条（回退+提示）
        snap = snapshots.get(name)  # {"sid":BCS店id, "prices":{nmID:price|None}}；无快照店则 None
        for it in items:
            price = it.get("price")  # 优先 WB 列表实时价
            reason, mode = "", "BCS"
            if snap is None:
                mode, reason = "WB回退", "该店无本地快照"
            else:
                if price is None:
                    price = snap["prices"].get(it["nmID"])  # 快照价（可能 None）
                if price is None:
                    mode = "WB回退"
                    reason = "本地快照/映射表中不存在或无价格"
                    if it.get("vc") and it["vc"] not in mapping_vcs:
                        reason += "（映射表中亦不存在）"
                else:  # 快照价非空 → 优先用快照价（WB 列表未返回价时）
                    if it.get("price") is None:
                        it["price"] = price
            it["_mode"] = mode
            it["_reason"] = reason
            it["_price"] = price
            rows.append({"店铺": name, "shopId": sid, "nmID": it["nmID"], "vendorCode": it["vc"],
                         "标题": it["title"], "原折扣": it["old_d"], "目标折扣": target,
                         "方式": mode, "原因": reason,
                         "结果": "DRY" if not args.apply else "待提交"})
            tag = f"[{mode}]" if mode != "BCS" else "[OK]"
            print(f"    {tag} nmID={it['nmID']} vc={it['vc']} {it['title']}  {it['old_d']}% -> {target}%"
                  + (f"  {reason}" if reason else ""))

        if args.apply:
            bcs_items = [it for it in items if it["_mode"] == "BCS"]
            wb_items = [it for it in items if it["_mode"] == "WB回退"]
            downgraded = False
            if bcs_items:
                print(f"  [提交] BCS 批量改 {len(bcs_items)} 条 ...")
                shop_bcs = snap["sid"] if snap else sid
                try:
                    res = bcs_batch_discount(shop_bcs, bcs_items, target)
                    for it in bcs_items:
                        r = res.get(it["nmID"], "未知")
                        for row in rows[-len(items):]:
                            if row["nmID"] == it["nmID"]:
                                row["结果"] = r
                        print(f"    [BCS] nmID={it['nmID']} {r}")
                except Exception as e:
                    print(f"  [警告] BCS 批量失败（{e}），该批降级为 WB 单条")
                    for it in bcs_items:
                        it["_mode"] = "WB回退"
                        it["_reason"] = "BCS批量失败降级"
                    downgraded = True
            if bcs_items and not downgraded:
                pass  # BCS 桶已处理
            else:
                wb_items = [it for it in items if it["_mode"] == "WB回退"]
            if wb_items:
                print(f"  [提交] WB 单条改 {len(wb_items)} 条（回退）...")
                for it in wb_items:
                    ok, rsn = change_discount(s, it["nmID"], target)
                    for row in rows[-len(items):]:
                        if row["nmID"] == it["nmID"]:
                            row["结果"] = "成功" if ok else f"失败:{rsn}"
                            if it["_mode"] == "WB回退":
                                row["方式"] = "WB回退"
                                row["原因"] = it["_reason"]
                    print(f"    [回退] nmID={it['nmID']} {'成功' if ok else '失败：' + rsn}"
                          + (f"  {it['_reason']}" if it['_reason'] else ""))
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
# -*- coding: utf-8 -*-
"""
wb_ops 远程仓（成都仓库）商品查询与删除

接口（抓包来源：api/网络请求/成都仓库货品查询和删除.md）：
- 仓库列表: BCS  GET /system/wbWarehouses/list?shopId=-1
     → 一次返回全部店铺仓库；返回的 id 与 marketplace 的 storeId/warehouseId 同一体系
     → 成都仓库 name=="成都仓库"（officeId=3006477），5 店各一个
- 查询:     GET marketplace.wildberries.ru/ns/marketplace-app/marketplace-remote-wh/api/v3/portal/stocks
     ?order=asc&stores={storeId}  → data.next 游标分页 + data.stocks[]
- 删除:     DELETE 同 URL，body {"warehouseId":storeId,"chrtId":xxx}
     → 一次一条 chrtId；响应 error:false 即成功（不可逆，永久删除）

鉴权：仓库列表走 BCS（bcs.py）；查询/删除走 WB 三件套（wb_api.py make_session）
约定：dry-run 默认；--apply 执行（不可逆，须同时 --yes 确认）；每店处理完做写后复查。
并发：--parallel N 跨店并行（店内串行，默认 1=串行），避免对 marketplace 同店并发触发限流。
"""
import csv
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import bcs
from . import common
from . import config
from . import credentials
from . import wb_api

STOCKS_URL = ("https://marketplace.wildberries.ru/ns/marketplace-app/"
              "marketplace-remote-wh/api/v3/portal/stocks")
CHENGDU_NAME = "成都仓库"


def list_chengdu_warehouses():
    """从 BCS 仓库列表筛出全部店铺「成都仓库」→ [{'shopId','id','name'}]"""
    whs = bcs.fetch_warehouses(-1)  # shopId=-1 一次返回全部
    return [w for w in whs if str(w.get("name", "")).strip() == CHENGDU_NAME]


def fetch_stocks(session, store_id, max_pages=200):
    """分页拉取某仓库全部库存条目 → list[dict]（含 article/chrtId/nmId/storeId/amount/name）
    游标分页：data.next 非空则继续；超过 max_pages 强制终止防死循环。"""
    items, nxt, page = [], "", 0
    while True:
        params = {"order": "asc", "stores": store_id}
        if nxt:
            params["next"] = nxt
        d = wb_api.request(session, "GET", STOCKS_URL, params=params)
        data = d.get("data") or {}
        chunk = data.get("stocks") or []
        items.extend(chunk)
        page += 1
        nxt = data.get("next")
        print(f"    [分页 {page}] 本页 {len(chunk)} 条，累计 {len(items)} 条", flush=True)
        if not nxt:
            break
        if page >= max_pages:
            print(f"    [警告] 超过 {max_pages} 页未结束，提前终止（数据异常）", flush=True)
            break
    return items


def delete_stock(session, warehouse_id, chrt_id):
    """删除仓库中单个 chrt（永久、不可逆）。返回 (ok, errText)"""
    try:
        r = wb_api.request(session, "DELETE", STOCKS_URL,
                           json={"warehouseId": warehouse_id, "chrtId": chrt_id})
        if r.get("error"):
            return False, r.get("errorText") or "删除失败(error=true)"
        return True, ""
    except common.CookieExpiredError:
        raise
    except Exception as e:
        return False, str(e)[:120]


_lock = threading.Lock()


def process_shop(shop, wid, cred, args, result_holder):
    """处理单个店铺成都仓库：查询→(dry-run 预览 | apply 逐条删除+复查)。
    独立线程内运行（店内串行、跨店并行）。结果追加到 result_holder（锁保护）。"""
    sid = shop["shopId"]
    print(f"\n=== {shop['shopName']}({sid}) 成都仓库 {wid} ===", flush=True)
    rows, t = [], {"total": 0, "deleted": 0, "failed": 0}
    try:
        session = wb_api.make_session(shop, cred.root_version)
    except common.CookieExpiredError as e:
        print(f"  [警告] {e}（该店跳过）", flush=True)
        return
    stocks = fetch_stocks(session, wid)
    t["total"] = len(stocks)
    if not stocks:
        print("  仓库为空")
        with _lock:
            result_holder.append((rows, t))
        return
    print(f"  [列表] 共 {len(stocks)} 条")
    if not args.apply:
        # dry-run：展示前 10 条预览
        for stk in stocks[:10]:
            print(f"    article={stk.get('article')} ch={stk.get('chrtId')} "
                  f"nmId={stk.get('nmId')} amount={stk.get('amount')}", flush=True)
        if len(stocks) > 10:
            print(f"    ... 其余 {len(stocks)-10} 条略", flush=True)
        for stk in stocks:
            rows.append({"店铺": shop["shopName"], "店铺ID": sid, "仓库ID": wid,
                         "article": stk.get("article"), "chrtId": stk.get("chrtId"),
                         "nmId": stk.get("nmId"), "amount": stk.get("amount"),
                         "结果": "DRY-将永久删除"})
        t["deleted"] = len(stocks)
    else:
        # apply：逐条删除（店内串行，避免同店并发限流）
        for i, stk in enumerate(stocks, 1):
            ok, err = delete_stock(session, wid, stk.get("chrtId"))
            res = "已删除" if ok else f"失败:{err}"
            if ok:
                t["deleted"] += 1
            else:
                t["failed"] += 1
            if i % 5 == 1 or ok is False:
                print(f"    [{i}/{len(stocks)}] ch={stk.get('chrtId')} "
                      f"article={stk.get('article')} -> {res}", flush=True)
            rows.append({"店铺": shop["shopName"], "店铺ID": sid, "仓库ID": wid,
                         "article": stk.get("article"), "chrtId": stk.get("chrtId"),
                         "nmId": stk.get("nmId"), "amount": stk.get("amount"),
                         "结果": res})
            time.sleep(args.interval)
        # 写后复查
        try:
            left = fetch_stocks(session, wid)
            print(f"  [复查] 删除后该仓库剩余 {len(left)} 条"
                  f"（{'清空 ✓' if not left else '仍有残留，见下方清单'}）", flush=True)
            for stk in left:
                print(f"    残留 article={stk.get('article')} ch={stk.get('chrtId')}", flush=True)
        except Exception as e:
            print(f"  [复查] 失败: {e}", flush=True)
    with _lock:
        result_holder.append((rows, t))


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已填 cookie 的店铺")
        return 1
    shop_map = {s["shopId"]: s for s in wb_shops}

    # ① 成都仓库列表（BCS）
    whs = list_chengdu_warehouses()
    if not whs:
        print("[错误] BCS 仓库列表未发现『成都仓库』")
        return 1
    pairs = [(w["shopId"], w["id"]) for w in whs if w["shopId"] in shop_map]
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        pairs = [p for p in pairs if p[0] in want]
    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）")
        return 1

    mode = "删除" if args.apply else "预览"
    print(f"成都仓库 {len(pairs)} 个: " + ", ".join(
        f"{shop_map[sid]['shopName']}({sid}->仓库{wid})" for sid, wid in pairs)
        + f" | 目标: {mode}{'' if args.yes else '（需 --yes 确认）'}"
        + (f" | 并发 {args.parallel} 店并行" if args.apply and args.parallel > 1 else ""), flush=True)

    if args.apply and not args.yes:
        print("[提示] 永久删除不可逆，请追加 --yes 确认后执行（避免后台挂起等待输入）")
        return 1

    # ② 按店处理：dry-run 串行 / apply 支持跨店并发
    results = []
    if args.apply and args.parallel > 1:
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            futs = [ex.submit(process_shop, shop_map[sid], wid, cred, args, results)
                    for sid, wid in pairs]
            for f in futs:
                f.result()
    else:
        for sid, wid in pairs:
            process_shop(shop_map[sid], wid, cred, args, results)

    rows, totals = [], {"total": 0, "deleted": 0, "failed": 0}
    for shop_rows, t in results:
        rows.extend(shop_rows)
        for k in totals:
            totals[k] += t[k]

    print(f"\n[汇总] 共 {totals['total']} | "
          f"{('删除 ' + str(totals['deleted'])) if args.apply else '预览 ' + str(len(rows))}"
          f" | 失败 {totals['failed']}")
    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"远程仓删除_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "店铺ID", "仓库ID",
                                              "article", "chrtId", "nmId", "amount", "结果"])
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")

    if not args.apply:
        print("\n（dry-run 未执行删除；确认清单后加 --apply --yes 永久删除）")
    return 0


def build_parser(sub):
    p = sub.add_parser("remote-wh", help="成都仓库商品永久删除（dry-run 默认，--apply --yes 执行）")
    p.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认全部 5 店成都仓）")
    p.add_argument("--limit", type=int, default=0, help="（预留）每店最多处理 N 条，0=全部")
    p.add_argument("--interval", type=float, default=0.3, help="删除请求间隔秒（默认 0.3）")
    p.add_argument("--parallel", type=int, default=1, help="并发店铺数（默认 1=串行；--apply 时有效，店内仍串行）")
    p.add_argument("--apply", action="store_true", help="真正执行删除（不可逆）")
    p.add_argument("--yes", action="store_true", help="确认永久删除（后台运行必备）")
    return p
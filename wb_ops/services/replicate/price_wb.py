# -*- coding: utf-8 -*-
"""
wb_ops WB 原生 dp-api 批量改价 (price_wb)

接口（抓包来源：api/网络请求/wb在线批量修改价格.har）：
- 提交: POST discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/upload/task
        ?checkChange=true|false
        条目 {"vendorCode","nmID","price":N,"currencyIsoCode":"CNY"}（只改价无 discount 字段；
        price/discount 同源，改折扣传 discount）
        响应 {"data":{"id":任务号,"alreadyExists":bool},"error":false}
- 二次确认: 降价过多时 checkChange=true 预检返回 data.priceModal/quarantineModal，
        确认后 checkChange=false 重发同一 body 才落库 —— 本实现预检后**自动确认提交**（用户确认的语义）。

定位：`price` 命令默认通道（2026-09-24 起，registry alias price-wb 同一实现）；
BCS price/batch 原通道保留为 `price-bcs` 备用（ops.run("price")，未动）。
Plan 层复用 ops_plan.plan_price（目标价：--price > 商品价格表 floor(双倍售价) > 映射表 dp）；
dry-run 复用 ops_plan.dry_run；≤原价/2 本地预拦截保留（WB quarantine hardLimit 同口径）；
auto_review 照用 ops_executor._auto_review_shop（隔离区 price-review 本就是 dp-api 同域）。

假设：currencyIsoCode 固定 "CNY"（全店国内站点；与 discount 实现兜底一致，未来扩站点再加参数）。

用法：
  wb.py price --name 充电宝 --price 130 --apply          # WB 原生批量改价
  wb.py price --vc BCS-XXX-123 --shops 9352 --price 99 --apply   # 单 vc 单店
  wb.py price --vc ... --discount 30 --keep-price --apply  # 保持现价只改折扣
"""
import time
from datetime import datetime

from wb_ops.adapters import wb_client as wb_api
from wb_ops import common
from wb_ops import credentials
from . import ops
from .ops_plan import (
    load_state,
    resolve_filters,
    plan_price,
    price_limit_violations,
    dry_run,
    RED, GREEN, YELLOW, RESET,
)
from .ops_executor import (
    RESULT_CSV,
    _normalize_csv_encoding,
    _rotate_result_csv_if_needed,
    _auto_review_shop,
)

PRICE_CHUNK = 100   # upload/task 单批条数（对齐 discount 命令默认 100）
MAX_CHUNK = 300     # chunk 夹取上限（discount help 实践：最大 300）
BATCH_SLEEP = 0.3
SHOP_SLEEP = 0.6
ACTION_TAG = "price_wb"   # ops_result.csv 操作列标签（与 BCS 通道的 "price" 区分）


def _filter_shops(args):
    """--shops 过滤（照抄 stock_wb._filter_shops）。"""
    all_shops = ops.get_shops()
    if not all_shops:
        print("[错误] 未找到任何店铺数据文件，请先运行 wb.py fetch")
        return None
    shops = [int(s) for s in args.shops.split(",")] if getattr(args, "shops", None) else all_shops
    missing = [s for s in shops if s not in all_shops]
    if missing:
        print(f"[警告] 以下店铺无数据文件（未 fetch）：{missing}，将跳过")
        shops = [s for s in shops if s in all_shops]
    return shops


def _build_payload(item, discount):
    """plan_price item → dp-api upload/task 条目。

    只改价：{"vendorCode","nmID","price","currencyIsoCode"}（HAR 实证无 discount 字段）；
    --discount 显式给出（含 0）时附加 discount 字段。
    """
    d = {"vendorCode": item["vc"], "nmID": int(item["nmID"]),
         "price": int(item["price"]), "currencyIsoCode": "CNY"}
    if discount is not None:
        d["discount"] = int(discount)
    return d


def exec_shop(client, plan, args, results, ts):
    """单店提交：≤原价/2 预拦截 → 分批 upload/task（预检后自动确认）。返回 (ok, fail)。"""
    sid = plan["shopId"]
    ok = fail = 0
    # ① ≤原价/2 预拦截（dry-run 阶段已黄标提示，apply 时剔除）
    bad = price_limit_violations(plan["items"])
    ok_items = plan["items"]
    if bad:
        print(f"  {RED}[跳过] {len(bad)} 条低于原价一半（WB quarantine 硬限）：{RESET}")
        for vc, cn, cur, target in bad[:10]:
            print(f"    {vc} | {cn} | 原价 {cur} → 目标 {target}")
        if len(bad) > 10:
            print(f"    ... 共 {len(bad)} 条")
        ok_items = [it for it in plan["items"]
                    if not any(it["vc"] == b[0] and it["price"] == b[3] for b in bad)]
    if not ok_items:
        print("  本店无有效改价项，跳过")
        return 0, 0

    chunk = max(1, min(int(getattr(args, "chunk", PRICE_CHUNK)), MAX_CHUNK))
    interval = float(getattr(args, "interval", BATCH_SLEEP))
    discount = getattr(args, "discount", None)
    for i in range(0, len(ok_items), chunk):
        part = ok_items[i:i + chunk]
        payload = [_build_payload(it, discount) for it in part]
        try:
            r = client.upload_batch_discount(payload, precheck=True)
            ok_flag = bool(r.success)
            msg = f"taskId={r.task_id}" if ok_flag else (r.error_message or "FAIL")
            if ok_flag and r.already_exists:
                msg += "（任务号已存在，WB 幂等合并）"
            modal = ""
            if ok_flag and (r.price_modal or r.quarantine_modal):
                tags = "、".join(t for t, on in (("降价提示", r.price_modal),
                                                 ("隔离区提示", r.quarantine_modal)) if on)
                modal = f" · 预检: {tags}（已自动确认并提交）"
        except common.CookieExpiredError:
            raise
        except Exception as e:
            ok_flag, msg, modal = False, f"FAIL: {e}", ""
        label = f"price_wb 批{i // chunk + 1}（{len(part)} 条）"
        if ok_flag:
            ok += len(part)
            print(f"  {GREEN}✓{RESET} {label} 成功: {msg}{modal}")
        else:
            fail += len(part)
            print(f"  {RED}✗{RESET} {label} 失败: {msg}")
        for it in part:
            results.append([sid, it["nmID"], ACTION_TAG, it["price"], msg, ts])
        if i + chunk < len(ok_items):
            time.sleep(interval)
    return ok, fail


def _report_zero_items(plans):
    zero = [(p["shopId"], it) for p in plans for it in p["items"] if it.get("orig_zero")]
    if not zero:
        return
    print(f"\n{RED}⚠ [0 值商品报告] {len(zero)} 项操作前价格为 0（WB 官方数据延迟或商品受限）{RESET}")
    print("  已照常提交修改；若之后查询仍为 0，属 WB 官方原因（修改生效可能延迟），无需反复修改。")
    seen = {}
    for sid, it in zero:
        d = seen.setdefault(it["vc"], {"cn": it.get("cn", ""), "shops": [],
                                       "old": it.get("cur_price"), "new": it["price"]})
        d["shops"].append(sid)
    for vc, d in sorted(seen.items()):
        print(f"    {vc} | {d['cn']} | 原值 {d['old']} → 目标 {d['new']} | 店 {','.join(map(str, d['shops']))}")


def _append_csv(results):
    """明细追加 ops_result.csv（复用 BCS 通道的编码归一化与按月归档）。"""
    import csv
    import os
    _normalize_csv_encoding()
    _rotate_result_csv_if_needed()
    need_header = not (os.path.exists(RESULT_CSV) and os.path.getsize(RESULT_CSV) > 0)
    os.makedirs(os.path.dirname(RESULT_CSV), exist_ok=True)
    with open(RESULT_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(["店铺", "ID(nmId/chrtId)", "操作", "目标值", "接口响应", "时间"])
        w.writerows(results)


def run(args):
    discount = getattr(args, "discount", None)
    club = getattr(args, "club_discount", None)
    if discount is not None and not (0 <= discount <= 100):
        print("[错误] --discount 需在 0-100 之间")
        return 1

    if getattr(args, "keep_price", False) and discount is None and club is None:
        print(f"{YELLOW}[警告] --keep-price 未配合 --discount，本次为 no-op（价格保持现值）{RESET}")
    if club is not None:
        print(f"{YELLOW}[警告] --club-discount 在 WB 原生通道暂不支持，已忽略；"
              f"需要 club 折扣请用 price-bcs{RESET}")

    vcs, desc = resolve_filters(args)
    if not vcs:
        print("[提示] 筛选结果为空，无操作")
        return 0
    print(f"目标: {desc}（{len(vcs)} 个 vendorCode）· 通道=WB 原生 dp-api（预检后自动确认降价/隔离区弹窗）")

    shops = _filter_shops(args)
    if shops is None:
        return 1

    state, _, boss = load_state()
    plans = plan_price(vcs, shops, state, boss, getattr(args, "price", None),
                       discount, club, keep_price=getattr(args, "keep_price", False))
    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法操作（上方 [跳过] / [跳过汇总] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / 无目标价。")
        return 0

    if not getattr(args, "apply", False):
        dry_run(plans, "price")
        return 0

    cred = credentials.get()
    ok = fail = 0
    results = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pi, p in enumerate(plans):
        sid = p["shopId"]
        print(f"\n>>> 店{sid}：price_wb（{len(p['items'])} 条）")
        shop = cred.wb_shop(sid)
        if not shop:
            print(f"  {RED}✗{RESET} 无 WB 凭证，跳过（{len(p['items'])} 条计入失败）")
            fail += len(p["items"])
            results.extend([sid, it["nmID"], ACTION_TAG, it["price"], "SKIP: 无 WB 凭证", ts]
                           for it in p["items"])
            continue
        try:
            client = wb_api.WBClient(shop, cred.root_version)
            o, f = exec_shop(client, p, args, results, ts)
            ok += o
            fail += f
            if getattr(args, "auto_review", False) and o:
                _auto_review_shop(sid, [it for it in p["items"]
                                        if not any(it["vc"] == b[0] and it["price"] == b[3]
                                                   for b in price_limit_violations(p["items"]))])
        except common.CookieExpiredError as e:
            print(f"  {RED}✗{RESET} cookie 失效，本店中止: {e}")
            fail += len(p["items"])
            results.extend([sid, it["nmID"], ACTION_TAG, it["price"], "SKIP: cookie 失效", ts]
                           for it in p["items"])
        if pi < len(plans) - 1:
            time.sleep(SHOP_SLEEP)

    _append_csv(results)
    print(f"\n结果: 成功 {ok} · 失败 {fail}（明细已保存 {RESULT_CSV}）")
    _report_zero_items(plans)

    if getattr(args, "sync", False):
        from wb_ops.services.catalog_svc import catalog_svc
        catalog_svc.post_write_merge()
    else:
        common.print_write_hint()
    return 0


# ---------- 规范门面（供其他脚本依赖；跨域调用走 replicate_svc.apply_prices_wb） ----------

def apply_prices(shop, items, *, root_version=None, chunk=PRICE_CHUNK,
                 interval=BATCH_SLEEP, precheck=True) -> dict:
    """单店批量改价（WB 原生 dp-api upload/task，最底层门面）。

    Args:
        shop: credentials wb_shop(sid) 店铺 dict（含 authorizev3/wb_seller_lk/cookie）
        items: [{"vendorCode": "...", "nmID": 123, "price": 99(, "discount": 30)}, ...]
        precheck: True = 预检后自动确认提交（checkChange=true→false；现默认语义）
    Returns:
        {"ok": int, "fail": int, "details": [{"vendorCode","nmID","price","ok","msg"}], "task_ids": [...]}
    异常：cookie 失效抛 common.CookieExpiredError，由调用方处理。
    """
    client = wb_api.WBClient(shop, root_version)
    chunk = max(1, min(int(chunk), MAX_CHUNK))
    ok = fail = 0
    details, task_ids = [], []
    for i in range(0, len(items), chunk):
        part = items[i:i + chunk]
        payload = [{"vendorCode": it["vendorCode"], "nmID": int(it["nmID"]),
                    "price": int(it["price"]), "currencyIsoCode": "CNY",
                    **({"discount": int(it["discount"])} if it.get("discount") is not None else {})}
                   for it in part]
        try:
            r = client.upload_batch_discount(payload, precheck=precheck)
            ok_flag = bool(r.success)
            msg = f"taskId={r.task_id}" if ok_flag else (r.error_message or "FAIL")
            if ok_flag and r.task_id:
                task_ids.append(r.task_id)
        except common.CookieExpiredError:
            raise
        except Exception as e:
            ok_flag, msg = False, f"FAIL: {e}"
        for it in part:
            details.append({"vendorCode": it["vendorCode"], "nmID": int(it["nmID"]),
                            "price": int(it["price"]), "ok": ok_flag, "msg": msg})
        ok += len(part) if ok_flag else 0
        fail += 0 if ok_flag else len(part)
        if i + chunk < len(items):
            time.sleep(interval)
    return {"ok": ok, "fail": fail, "details": details, "task_ids": task_ids}

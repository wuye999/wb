# -*- coding: utf-8 -*-
"""
wb_ops 批量操作执行器 (ops_executor)
负责分批调用 BCS 接口提交改价、改库存、下架操作，自动处理编码转换、日志记录与审核触发。
"""
import csv
import os
import shutil
import time
from datetime import datetime

from wb_ops.adapters import bcs_client as bcs
from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import wb_client as wb_api
from .ops_plan import price_limit_violations, price_review_items, PRICE_HALF_LIMIT_NOTE

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

PRICE_CHUNK = 300
STOCK_CHUNK = 200
BATCH_SLEEP = 0.15
SHOP_SLEEP = 0.6
RESULT_CSV = config.RESULT_CSV


def _auto_review_shop(sid, items):
    """改价后自动审核：按 nmID 匹配「降价 30-49.9%」的商品，调隔离区审核接口应用新价格。
    返回成功审核数。"""
    review_items = price_review_items(items)
    if not review_items:
        return 0
    shop = credentials.get().wb_shop(sid)
    if not shop:
        print(f"  [自动审核] 店铺 {sid} 无 WB 凭证，跳过")
        return 0
    try:
        session = wb_api.make_session(shop)
        quarantine = price_review.fetch_all_quarantine(session)
        nm_to_id = {it.get("nmID"): it.get("id") for it in quarantine if it.get("nmID")}
        ids = []
        matched_nm = []
        for it in review_items:
            qid = nm_to_id.get(it.get("nmID"))
            if qid:
                ids.append(qid)
                matched_nm.append(it["nmID"])
        if not ids:
            print(f"  [自动审核] 降价 30-49.9% 的 {len(review_items)} 个商品未在隔离区列表（可能尚未进入审查），跳过")
            return 0
        d = price_review.apply_prices(session, ids)
        ok = not d.get("error")
        print(f"  [自动审核] 应用新价格 {len(ids)} 个 nmID={matched_nm}"
              + (" ✓成功" if ok else f" ✗失败:{d.get('errorText')}"))
        return len(ids) if ok else 0
    except common.CookieExpiredError as e:
        print(f"  [自动审核] 店铺 {sid} cookie 失效，跳过: {e}")
        return 0
    except Exception as e:
        print(f"  [自动审核] 店铺 {sid} 失败: {e}")
        return 0


def _normalize_csv_encoding():
    if not os.path.exists(RESULT_CSV) or os.path.getsize(RESULT_CSV) == 0:
        return
    try:
        with open(RESULT_CSV, "r", encoding="utf-8-sig") as f:
            f.read()
        return
    except UnicodeDecodeError:
        pass
    raw = open(RESULT_CSV, "rb").read().splitlines()
    lines = []
    for ln in raw:
        try:
            lines.append(ln.decode("utf-8-sig"))
        except UnicodeDecodeError:
            lines.append(ln.decode("gb18030"))
    os.makedirs(os.path.dirname(RESULT_CSV), exist_ok=True)
    shutil.copy2(RESULT_CSV, RESULT_CSV + ".bak")
    with open(RESULT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"  ⚠ ops_result.csv 非 UTF-8 编码（可能被 Excel 另存过），已备份 {RESULT_CSV}.bak 并转码为 UTF-8-SIG")


def _endpoint(body):
    if "dataList" in body:
        return "price/batch"
    if "warehouses" in body:
        return "stock/batchSetByChrtIdsBatch"
    if "nmIds" in body:
        return "clean/removeToTrash"
    raise RuntimeError("无法识别操作类型")


def _post(body, label):
    """提交写操作，返回 (ok: bool, msg: str)。ok=False 表示接口失败/异常，供 run_apply 准确计数。
    ★ 2026-09-03 BCS 接口变更适配（内部 body 结构不变，提交前按端点转换）：
    - price/batch：shopId 须在每个 dataList 条目内（顶层 shopId 已不被识别，报 500 null key）
    - clean/removeToTrash：body 须为根级数组 [{shopId, nmId}]（每条目一个 nmId）"""
    try:
        ep = _endpoint(body)
        if ep == "price/batch":
            payload = {"dataList": [{**it, "shopId": body["shopId"]} for it in body["dataList"]]}
        elif ep == "clean/removeToTrash":
            payload = [{"shopId": body["shopId"], "nmId": nm} for nm in body["nmIds"]]
        else:
            payload = body
        r = bcs.http_post_json(f"{bcs.base_url()}/shopKeeper/{ep}", payload)
        msg = r.get("msg", "")
        if r.get("code") == 200:
            print(f"  {GREEN}✓{RESET} {label} 成功: {msg}")
            return True, (msg or "OK")
        print(f"  {RED}✗{RESET} {label} 失败: code={r.get('code')} {msg}")
        return False, f"FAIL: {msg}"
    except Exception as e:
        print(f"  {RED}✗{RESET} {label} 异常: {e}")
        return False, f"FAIL: {e}"


def run_apply(plans, action, auto_review=False):
    ok = fail = 0
    skipped_bad = 0
    zero_items = []
    results = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pi, p in enumerate(plans):
        sid = p["shopId"]
        print(f"\n>>> 店{sid}：{action}")
        if action == "price":
            bad = price_limit_violations(p["items"])
            if bad:
                print(f"  {RED}[跳过] {len(bad)} 条{PRICE_HALF_LIMIT_NOTE}：{RESET}")
                for vc, cn, cur, target in bad[:10]:
                    print(f"    {vc} | {cn} | 原价 {cur} → 目标 {target}")
                if len(bad) > 10:
                    print(f"    ... 共 {len(bad)} 条")
                skipped_bad += len(bad)
            ok_items = [it for it in p["items"] if not any(it["vc"] == b[0] and it["price"] == b[3] for b in bad)]
            zero_items.extend((sid, it["vc"], it.get("cn", ""), it.get("cur_price"), it["price"])
                              for it in ok_items if it.get("orig_zero"))
            dl = [{"nmID": it["nmID"], "price": it["price"],
                   "discount": it["discount"], "clubDiscount": it["clubDiscount"]}
                  for it in ok_items]
            if not dl:
                print("  本店无有效改价项，跳过")
                continue
            for i in range(0, len(dl), PRICE_CHUNK):
                body = {"shopId": sid, "dataList": dl[i:i + PRICE_CHUNK]}
                ok_flag, msg = _post(body, f"{action} 批{i // PRICE_CHUNK + 1}")
                if ok_flag:
                    ok += len(body["dataList"])
                else:
                    fail += len(body["dataList"])
                for it in body["dataList"]:
                    results.append([sid, it["nmID"], action, it["price"], msg, ts])
                if i + PRICE_CHUNK < len(dl):
                    time.sleep(BATCH_SLEEP)
            if auto_review:
                _auto_review_shop(sid, ok_items)
        elif action == "stock":
            for wh in p["warehouses"]:
                sis = wh["stockItems"]
                for i in range(0, len(sis), STOCK_CHUNK):
                    body = {"shopId": sid,
                            "warehouses": [{"warehouseId": wh["warehouseId"],
                                            "stockItems": sis[i:i + STOCK_CHUNK]}]}
                    ok_flag, msg = _post(body, f"stock 仓库{wh['warehouseId']} 批{i // STOCK_CHUNK + 1}")
                    if ok_flag:
                        ok += len(sis[i:i + STOCK_CHUNK])
                    else:
                        fail += len(sis[i:i + STOCK_CHUNK])
                    for it in sis[i:i + STOCK_CHUNK]:
                        results.append([sid, it["chrtId"], action, it["amount"], msg, ts])
                    if i + STOCK_CHUNK < len(sis):
                        time.sleep(BATCH_SLEEP)
            zero_items.extend((sid, it["vc"], it.get("cn", ""), it.get("cur_amt"), it["amount"])
                              for it in p["items"] if it.get("orig_zero"))
        elif action == "trash":
            by_wh = {}
            spec_total = 0
            for it in p["items"]:
                for chrt, wh in it.get("stock_specs", []):
                    by_wh.setdefault(wh, []).append(chrt)
            clear_fail = []
            for wh, chrt_list in sorted(by_wh.items()):
                for i in range(0, len(chrt_list), STOCK_CHUNK):
                    body = {"shopId": sid,
                            "warehouses": [{"warehouseId": wh,
                                            "stockItems": [{"chrtId": c, "amount": 0}
                                                           for c in chrt_list[i:i + STOCK_CHUNK]]}]}
                    ok_flag, _msg = _post(body, f"清库存 仓库{wh} 批{i // STOCK_CHUNK + 1}")
                    if ok_flag:
                        spec_total += len(chrt_list[i:i + STOCK_CHUNK])
                    else:
                        clear_fail.extend(chrt_list[i:i + STOCK_CHUNK])
            if by_wh:
                print(f"  [清库存] 下架前已提交 {spec_total} 个规格库存归零" +
                      (f"，{len(clear_fail)} 个失败" if clear_fail else ""))
            nms = p["nmIds"]
            for i in range(0, len(nms), PRICE_CHUNK):
                body = {"shopId": sid, "nmIds": nms[i:i + PRICE_CHUNK]}
                ok_flag, msg = _post(body, f"trash 批{i // PRICE_CHUNK + 1}")
                if ok_flag:
                    ok += len(body["nmIds"])
                else:
                    fail += len(body["nmIds"])
                for nm in body["nmIds"]:
                    results.append([sid, nm, action, "", msg, ts])
                if i + PRICE_CHUNK < len(nms):
                    time.sleep(BATCH_SLEEP)
            if clear_fail:
                print(f"  {RED}⚠ [清库存失败] {len(clear_fail)} 个规格库存未清零仍已下架（可能 WB 受限/延迟）{RESET}")
                print(f"    chrtId: {clear_fail[:10]}{'...' if len(clear_fail) > 10 else ''}")
                results.extend([sid, c, "stock_clear_fail", 0, "FAIL", ts] for c in clear_fail)
        if pi < len(plans) - 1:
            time.sleep(SHOP_SLEEP)

    _normalize_csv_encoding()
    need_header = not (os.path.exists(RESULT_CSV) and os.path.getsize(RESULT_CSV) > 0)
    os.makedirs(os.path.dirname(RESULT_CSV), exist_ok=True)
    with open(RESULT_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if need_header:
            w.writerow(["店铺", "ID(nmId/chrtId)", "操作", "目标值", "接口响应", "时间"])
        w.writerows(results)
    summary = f"结果: 成功 {ok} · 失败 {fail}"
    if skipped_bad:
        summary += f" · {RED}价格下限剔除 {skipped_bad}{RESET}"
    print(f"\n{summary}（明细已保存 {RESULT_CSV}）")

    if zero_items:
        print(f"\n{RED}⚠ [0 值商品报告] {len(zero_items)} 项操作前价格/库存为 0（WB 官方数据延迟或商品受限）{RESET}")
        print("  已照常提交修改；若之后查询仍为 0，属 WB 官方原因（修改生效可能延迟），无需反复修改。")
        seen = {}
        for sid, vc, cn, old, new in zero_items:
            seen.setdefault(vc, {"cn": cn, "shops": [], "old": old, "new": new})["shops"].append(sid)
        for vc, d in sorted(seen.items()):
            print(f"    {vc} | {d['cn']} | 原值 {d['old']} → 目标 {d['new']} | 店 {','.join(map(str, d['shops']))}")
    return ok, fail

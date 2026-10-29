# -*- coding: utf-8 -*-
"""wb_ops 移回收站 WB 原生接口门面 (trash_wb)

接口：POST https://seller-content.wildberries.ru/ns/source/content-card/source/v2/moveNmsToTrash
      body 为裸数组 [{"nmID": int}, ...]；
      响应 {"data":{},"error":false,"errorText":"","additionalErrors":{}}

抓包实证（api/网络请求/wb批量移至回收站.har，2026-10-29）：
  仅需 WB 三件套（AuthorizeV3 + Seller-Lk + root-version），**不需要 BCS**；
  该接口**不清库存**、**不返回成功条数**，只能靠 error + additionalErrors 判定；
  有库存/在途的商品可能被 WB 拒收 ⇒ 故 trash 流程仍保留「先清库存」两阶段。

定位：`trash` / `trash-wb` 的默认通道。BCS `shopKeeper/clean/removeToTrash`（`trash-bcs`）
是**旧通道，仅供排查，不作兜底** —— WB 通道失败或 cookie 过期时**不自动改走 BCS**，
只把错误报出来（BCS 有 19s 限流窗口、成功率不可靠）。本模块只做底层门面，
不做计划/dry-run/CSV（那些由 ops / ops_plan / ops_executor 统一负责）。

与 banned.py:move_to_trash 的关系：逻辑同源（同一接口、同一 additionalErrors 解析），
此处复制并参数化，**故意不复用**——避免「换通道」牵连 `wb.py banned` 的既有行为。
"""
import time

from wb_ops import common
from wb_ops.adapters import wb_client as wb_api

WB_MOVE_TRASH = ("https://seller-content.wildberries.ru/ns/source/"
                 "content-card/source/v2/moveNmsToTrash")

CHUNK = 300        # 单批 nmID 上限（项目硬性 ≤300/批）
MAX_CHUNK = 300    # chunk 参数硬夹取上限
INTERVAL = 0.3     # 批间间隔秒
ACTION_TAG = "trash_wb"   # ops_result.csv「操作」列标签（BCS 通道为 trash_bcs）


def _dedupe(nm_ids):
    """去重保序。同一批内重复 nmID 无意义，且可能触发平台校验失败。"""
    seen, out = set(), []
    for n in nm_ids:
        n = int(n)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def parse_move_response(d, chunk):
    """响应 → {nmID: 失败原因}；返回空 dict 表示整批成功。纯函数，可离线单测。

    判定顺序：
      ① error=true              → 整批失败（errorText）
      ② additionalErrors 为空    → 整批成功
      ③ additionalErrors 非空    → 键名可转 int 的按 nmID 记；errors[] 取 params["1"]；
         若**全部**错误都能归属到具体 nmID ⇒ 未列出的 nmID 视为成功（按条记账才准）；
         若存在**无法归属**的键/项（或一条都解不出）⇒ 整批计失败（保守：接口不返回
         成功条数，宁可报失败让人复核，也不静默当成功），msg 带原始键值摘要。
    """
    chunk = list(chunk)
    if d.get("error"):
        reason = d.get("errorText") or "error=true"
        return {n: reason for n in chunk}
    add = d.get("additionalErrors") or {}
    if not add:
        return {}
    failed, unmapped = {}, []
    for k, v in add.items():
        if k == "errors":
            continue
        try:
            failed[int(k)] = str(v)
        except (TypeError, ValueError):
            unmapped.append(f"{k}:{v}")
    for e in add.get("errors") or []:
        try:
            nid = (e.get("params") or {}).get("1")
            if nid:
                failed[int(nid)] = e.get("trKey") or "未知原因"
        except (TypeError, ValueError):
            unmapped.append(str(e)[:60])
    if unmapped or not failed:
        summary = ("；".join(unmapped) or "未知原因")[:80]
        for n in chunk:
            failed.setdefault(n, summary)
    return failed


def move_nms(shop, nm_ids, *, root_version=None, chunk=CHUNK, interval=INTERVAL):
    """单店批量移回收站（WB 原生 moveNmsToTrash）。契约对齐 stock_wb.set_stock。

    Args:
        shop: credentials.wb_shop(sid) 的店铺 dict（含 authorizev3/wb_seller_lk/cookie）
        nm_ids: nmId 列表（自动去重保序）
        chunk: 单批条数（自动夹取 ≤300）
        interval: 批间间隔秒

    Returns:
        {"ok": int, "fail": int, "aborted": str|None,
         "details": [{"nmID": int, "ok": bool, "msg": str}, ...]}   # 每条 nmID 一行

    异常策略（与 stock_wb.set_stock 的「透传 CookieExpiredError」不同）：
        cookie 失效**不外抛**，而是把该批及剩余 nmID 记为
        "SKIP: cookie 失效（本店中止）" 并置 aborted。
        原因：一次调用跨多批，外抛会让调用方把已成功下架的 nmID 也误记为失败。
    """
    ids = _dedupe(nm_ids)
    chunk = max(1, min(int(chunk), MAX_CHUNK))
    details, aborted = [], None

    try:
        session = wb_api.make_session(shop, root_version)
    except common.CookieExpiredError as e:
        aborted = str(e) or "cookie 失效"
        details = [{"nmID": n, "ok": False, "msg": "SKIP: cookie 失效（本店中止）"} for n in ids]
        return {"ok": 0, "fail": len(details), "details": details, "aborted": aborted}
    except Exception as e:
        aborted = str(e) or "建会话失败"
        details = [{"nmID": n, "ok": False, "msg": f"SKIP: {aborted}"[:80]} for n in ids]
        return {"ok": 0, "fail": len(details), "details": details, "aborted": aborted}

    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            d = wb_api.request_post(session, WB_MOVE_TRASH,
                                    [{"nmID": n} for n in part], allow_400_json=True)
            bad = parse_move_response(d or {}, part)
        except common.CookieExpiredError as e:
            aborted = str(e) or "cookie 失效"
            details.extend({"nmID": n, "ok": False, "msg": "SKIP: cookie 失效（本店中止）"}
                           for n in ids[i:])
            break
        except Exception as e:
            bad = {n: f"{type(e).__name__}: {e}"[:80] for n in part}

        for n in part:
            good = n not in bad
            details.append({"nmID": n, "ok": good,
                            "msg": "OK" if good else (bad[n] or "未知原因")})
        if i + chunk < len(ids):
            time.sleep(interval)

    ok = sum(1 for x in details if x["ok"])
    return {"ok": ok, "fail": len(details) - ok, "details": details, "aborted": aborted}

"""WB 移回收站请求及逐商品受理判定，供下架和禁售商品命令复用。"""
import time

from sellerops.core import utils as common
from sellerops.integrations.wildberries import client as wb_api

WB_MOVE_TRASH = ("https://seller-content.wildberries.ru/ns/source/"
                 "content-card/source/v2/moveNmsToTrash")

CHUNK = 300        # 单批 nmID 上限（项目硬性 ≤300/批）
MAX_CHUNK = 300    # chunk 参数硬夹取上限
INTERVAL = 0.3     # 批间间隔秒


def parse_move_response(d, chunk):
    """响应 → {nmID: (状态, 原因)}；未列出的商品已受理。

    判定顺序：
      ① error=true              → 整批失败（errorText）
      ② additionalErrors 为空    → 整批成功
      ③ additionalErrors 非空    → 键名可转 int 的按 nmID 记；errors[] 取 params["1"]；
         若**全部**错误都能归属到具体 nmID ⇒ 未列出的 nmID 视为成功（按条记账才准）；
         若存在**无法归属**的键/项（或一条都解不出）⇒ 整批计失败（保守：接口不返回
         成功条数，宁可报失败让人复核，也不静默当成功），msg 带原始键值摘要。
    """
    chunk = list(chunk)
    if not isinstance(d, dict) or type(d.get("error")) is not bool:
        return dict.fromkeys(chunk, ("unknown", "未返回明确状态"))
    if d["error"]:
        reason = d.get("errorText") or "error=true"
        return dict.fromkeys(chunk, ("failed", reason))
    add = d.get("additionalErrors")
    if add is None or add == {}:
        return {}
    if not isinstance(add, dict) or not isinstance(add.get("errors", []), list):
        return dict.fromkeys(chunk, ("unknown", "无法识别商品回执"))
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
            else:
                unmapped.append("回执缺少商品编号")
        except (AttributeError, TypeError, ValueError):
            unmapped.append(str(e)[:60])
    if set(failed) - set(chunk):
        return dict.fromkeys(chunk, ("unknown", "包含清单以外的商品编号"))
    failed = {nm: ("failed", reason) for nm, reason in failed.items()}
    if unmapped or not failed:
        summary = ("；".join(unmapped) or "未知原因")[:80]
        for n in chunk:
            failed.setdefault(n, ("unknown", summary))
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
    ids = list(dict.fromkeys(map(int, nm_ids)))
    try:
        session = wb_api.make_session(shop, root_version)
    except common.CookieExpiredError as e:
        aborted = str(e) or "cookie 失效"
        details = [{"nmID": n, "ok": False, "status": "blocked", "msg": "SKIP: cookie 失效（本店中止）"} for n in ids]
        return {"ok": 0, "fail": len(details), "details": details, "aborted": aborted}
    except Exception as e:
        aborted = str(e) or "建会话失败"
        details = [{"nmID": n, "ok": False, "status": "blocked", "msg": f"SKIP: {aborted}"[:80]} for n in ids]
        return {"ok": 0, "fail": len(details), "details": details, "aborted": aborted}

    try:
        return move_in_session(session, ids, chunk=chunk, interval=interval)
    finally:
        if hasattr(session, "close"):
            session.close()


def move_in_session(session, nm_ids, *, chunk=CHUNK, interval=INTERVAL):
    ids = list(dict.fromkeys(map(int, nm_ids)))
    chunk = max(1, min(int(chunk), MAX_CHUNK))
    details, aborted = [], None
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            d = wb_api.request_post(session, WB_MOVE_TRASH,
                                    [{"nmID": n} for n in part], allow_400_json=True)
            bad = parse_move_response(d or {}, part)
        except common.CookieExpiredError as e:
            aborted = str(e) or "cookie 失效"
            details.extend({"nmID": n, "ok": False, "status": "blocked", "msg": "SKIP: cookie 失效（本店中止）"}
                           for n in ids[i:])
            break
        except Exception as e:
            bad = dict.fromkeys(part, ("unknown", f"{type(e).__name__}: {e}"[:80]))

        for n in part:
            status, message = bad.get(n, ("accepted", ""))
            details.append({"nmID": n, "ok": status == "accepted", "status": status, "msg": message})
        if i + chunk < len(ids):
            time.sleep(interval)

    ok = sum(1 for x in details if x["ok"])
    return {"ok": ok, "fail": len(details) - ok, "details": details, "aborted": aborted}

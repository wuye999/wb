"""BCS 写入回执与下架的清零、移回收站阶段。"""
import time
from collections.abc import Callable

from sellerops.core import utils as common
from sellerops.core.errors import AuthenticationError, PlatformApiError, RateLimitError
from sellerops.features.products.results import ProductResult
from sellerops.features.products.trash_execution import clear_stock_for_trash
from sellerops.integrations.bcs.client import client as bcs
from sellerops.persistence.product_results import ProductReceipt, append_product_history, write_product_receipts
from sellerops.settings import credentials, paths as config

PRICE_CHUNK = 300
STOCK_CHUNK = 200
BATCH_SLEEP = 0.15
SHOP_SLEEP = 0.6
TRASH_CHUNK = 300
TRASH_INTERVAL = 0.3


def bcs_receipt(method: Callable, *args) -> tuple[str, str, str | int | None]:
    try:
        reply = method(*args)
    except AuthenticationError:
        raise
    except RateLimitError as error:
        return "failed", str(error), None
    except PlatformApiError as error:
        status = "failed" if error.status_code and 400 <= error.status_code < 500 else "unknown"
        return status, str(error), None
    except Exception as error:
        return "unknown", f"BCS 回执不可用（{type(error).__name__}）", None
    if not isinstance(reply, dict):
        return "unknown", "BCS 未返回有效回执", None
    code = reply.get("code")
    detail = str(reply.get("msg") or "")
    data = reply.get("data") if isinstance(reply.get("data"), dict) else {}
    task = reply.get("taskId") or data.get("taskId") or data.get("id")
    task = task if type(task) in (str, int) else None
    if str(code) in {"401", "403"}:
        raise AuthenticationError(f"BCS 鉴权失败（{code}）")
    if str(code) == "200":
        return "accepted", detail, task
    if type(code) is int or (isinstance(code, str) and code.isdecimal()):
        return "failed", f"code={code} {detail}".strip(), task
    return "unknown", "BCS 回执未确认受理状态", task


def submit_bcs_prices(sid: int, items: list[dict], result: ProductResult, *,
                      chunk: int = PRICE_CHUNK) -> list[dict]:
    accepted = []
    for offset in range(0, len(items), chunk):
        batch = items[offset:offset + chunk]
        # BCS 需要商品级价格。未授权的折扣字段沿用接口的空值语义。
        payload = [{key: item.get(key) for key in ("nmID", "price", "discount", "clubDiscount")} for item in batch]
        try:
            status, msg, task = bcs_receipt(bcs.batch_set_prices, sid, payload)
        except AuthenticationError as error:
            for item in items[offset:]:
                result.record(sid, item, "blocked", str(error))
            break
        for item in batch:
            result.record(sid, item, status, msg, task)
        if status == "accepted":
            accepted.extend(batch)
        if offset + len(batch) < len(items):
            time.sleep(BATCH_SLEEP)
    return accepted


def submit_bcs_stock(plan: dict, result: ProductResult) -> None:
    sid = plan["shopId"]
    stock = plan["warehouses"][0]
    by_spec = {item["chrtId"]: item for item in plan["items"]}
    rows = stock["stockItems"]
    for offset in range(0, len(rows), STOCK_CHUNK):
        batch = rows[offset:offset + STOCK_CHUNK]
        try:
            status, msg, task = bcs_receipt(bcs.batch_set_stock, sid, stock["warehouseId"], batch)
        except AuthenticationError as error:
            for item in rows[offset:]:
                result.record(sid, by_spec[item["chrtId"]], "blocked", str(error))
            return
        for item in batch:
            result.record(sid, by_spec[item["chrtId"]], status, msg, task)
        if offset + len(batch) < len(rows):
            time.sleep(BATCH_SLEEP)


def apply_trash(plans: list[dict], *, channel: str, chunk: int | None = None,
                interval: float | None = None, outcome: dict | None = None) -> tuple[int, int]:
    """按原顺序先确认所有规格清零，再提交移回收站。"""
    from sellerops.features.products import trash

    common.validate_batch_options(chunk=chunk, interval=interval)
    receipts = []
    tag = f"trash_{channel}"
    step = min(chunk or TRASH_CHUNK, TRASH_CHUNK)
    gap = TRASH_INTERVAL if interval is None else interval
    for plan in plans:
        sid = plan["shopId"]
        ready, stopped = clear_stock_for_trash(sid, plan["items"])
        for item in [*plan.get("blocked_items", []), *stopped]:
            receipts.append(ProductReceipt(sid, item, tag, "", "blocked", item["reason"]))
            print(f"  [已阻止] 店{sid} {item.get('vc', '')}：{item['reason']}")
        by_id = {item["nmID"]: item for item in ready}
        if not by_id:
            continue
        ids = list(by_id)
        if channel == "wb":
            shop = credentials.get().wb_shop(sid)
            if not shop:
                receipts.extend(ProductReceipt(sid, item, tag, "", "blocked", "无 WB 凭证") for item in ready)
                continue
            reply = trash.move_nms(shop, ids, chunk=step, interval=gap)
            for detail in reply["details"]:
                status = detail["status"]
                receipts.append(ProductReceipt(sid, by_id[detail["nmID"]], tag, "", status, detail["msg"]))
        else:
            for offset in range(0, len(ids), step):
                batch = ids[offset:offset + step]
                try:
                    status, msg, task = bcs_receipt(bcs.remove_to_trash, sid, batch)
                except AuthenticationError as error:
                    receipts.extend(ProductReceipt(sid, by_id[nm], tag, "", "blocked", str(error))
                                    for nm in ids[offset:])
                    break
                receipts.extend(ProductReceipt(sid, by_id[nm], tag, "", status, msg, task) for nm in batch)
                if offset + len(batch) < len(ids):
                    time.sleep(gap)
        time.sleep(SHOP_SLEEP)
    write_product_receipts(config.current, receipts)
    append_product_history(config.current, receipts)
    if outcome is not None:
        outcome['receipts'] = [{'shop_id': r.shop_id, 'vc': r.item.get('vc'), 'nmId': r.item.get('nmID'),
                                'status': r.status, 'task_id': r.task_id, 'detail': r.detail} for r in receipts]
    ok = sum(r.status == "accepted" for r in receipts)
    fail = len(receipts) - ok
    print(f"\n结果：移回收站已受理 {ok} 个商品卡，失败或阻止 {fail} 个")
    return ok, fail

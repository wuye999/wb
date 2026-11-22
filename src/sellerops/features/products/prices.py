"""按商品计划提交 WB 价格与折扣。"""
import time

from sellerops.core import utils as common
from sellerops.core.errors import AuthenticationError
from sellerops.features.catalog.selection import load_state, resolve_filters, selected_shops
from sellerops.features.products.plans import dry_run, plan_price
from sellerops.features.products.price_review import review_submitted_prices
from sellerops.features.products.product_data import ProductDataResolver
from sellerops.features.products.results import ProductResult
from sellerops.integrations.wildberries import client as wb_api
from sellerops.settings import credentials

PRICE_CHUNK = 100
MAX_CHUNK = 300
BATCH_SLEEP = 0.3
SHOP_SLEEP = 0.6


def _build_payload(item: dict) -> dict:
    payload = {"vendorCode": item["vc"], "nmID": item["nmID"],
               "currencyIsoCode": item.get("currencyIsoCode") or "CNY"}
    for name in ("price", "discount"):
        if item.get(name) is not None:
            payload[name] = item[name]
    return payload


def submit_wb_prices(client, plan: dict, chunk: int, interval: float,
                     result: ProductResult) -> tuple[list[dict], bool]:
    sid, items = plan["shopId"], plan["items"]
    accepted = []
    for offset in range(0, len(items), chunk):
        batch = items[offset:offset + chunk]
        stopped = False
        task_id = None
        try:
            reply = client.upload_batch_discount([_build_payload(item) for item in batch])
            task_id = reply.task_id
            status = "accepted" if reply.success and task_id else getattr(reply, "outcome", "unknown")
            if status not in {"accepted", "failed", "unknown"} or (status == "accepted" and not task_id):
                status = "unknown"
            message = str(reply.error_message or "缺少明确受理回执") if status != "accepted" else ""
            if status == "accepted" and getattr(reply, "already_exists", False) is True:
                message = "WB 任务号已存在"
            stopped = getattr(reply, "auth_stopped", False) is True
        except AuthenticationError as error:
            status, message, stopped = "blocked", str(error), True
        except Exception as error:
            status, message = "unknown", f"WB 提交回执不可用（{type(error).__name__}）"
        for item in batch:
            result.record(sid, item, status, message, task_id)
        if status == "accepted":
            accepted.extend(batch)
        if stopped:
            for item in items[offset + len(batch):]:
                result.record(sid, item, "blocked", message)
            return accepted, True
        if offset + len(batch) < len(items):
            time.sleep(interval)
    return accepted, False


def run(args) -> int:
    common.validate_batch_options(chunk=getattr(args, "chunk", None), interval=getattr(args, "interval", None))
    return ProductResult("price", "wb", args).run(_run, args)


def _run(args, result: ProductResult) -> None:
    shops = result.shops = selected_shops(args)
    state, shop_rows, book = load_state(shops, all_products=getattr(args, "all_products", False),
                                       fresh=(result.apply and getattr(args, 'price', None) is None
                                              and not getattr(args, 'keep_price', False)
                                              and (getattr(args, 'from_price_book', False)
                                                   or getattr(args, 'discount', None) is None)))
    vcs, desc = resolve_filters(args, state, book)
    print(f"目标：{desc}，通道 WB 后台")
    with ProductDataResolver() as resolver:
        plans = plan_price(vcs, shops, state, getattr(args, "price", None), getattr(args, "discount", None),
                           None, keep_price=getattr(args, "keep_price", False), resolver=resolver,
                           allow_discount_only=True, shop_rows=shop_rows,
                           from_price_book=getattr(args, "from_price_book", False))
    result.add_plans(plans)
    if not result.apply:
        dry_run(plans, "price")
        return

    cred = credentials.get()
    for plan in plans:
        if not plan["items"]:
            continue
        sid = plan["shopId"]
        shop = cred.wb_shop(sid)
        try:
            if not shop:
                raise AuthenticationError("无 WB 凭证")
            client = wb_api.WBClient(shop, cred.root_version)
        except Exception as error:
            for item in plan["items"]:
                result.record(sid, item, "blocked", f"WB 会话不可用（{type(error).__name__}）")
            continue
        try:
            accepted, stopped = submit_wb_prices(client, plan, min(getattr(args, "chunk", PRICE_CHUNK), MAX_CHUNK),
                                                 getattr(args, "interval", BATCH_SLEEP), result)
            if result.review_requested:
                review_submitted_prices(sid, accepted, result, stopped=stopped)
        finally:
            session = getattr(client, "session", None)
            if session is not None:
                session.close()
        time.sleep(SHOP_SLEEP)
    common.print_write_hint()

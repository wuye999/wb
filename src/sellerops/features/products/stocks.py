"""预览和提交本店莫斯科仓的全部目标规格。"""
import time

from sellerops.core import utils as common
from sellerops.core.errors import AuthenticationError
from sellerops.domain.snapshot_values import positive_id, quantity
from sellerops.features.catalog.selection import load_state, selected_shops, resolve_filters
from sellerops.features.products.operations import confirm_irreversible
from sellerops.features.products.plans import plan_stock, dry_run, report_plan_skips
from sellerops.features.products.product_data import ProductDataResolver
from sellerops.features.products.results import ProductResult
from sellerops.integrations.wildberries import client as wb_api
from sellerops.integrations.wildberries import stocks as wsc
from sellerops.settings import credentials

STOCK_CHUNK = 500
BATCH_SLEEP = 0.3
SHOP_SLEEP = 0.6


def _live_stock_map(session, max_pages, warehouse_id):
    """只读已确认莫斯科仓；库存缺行只表示未知，不决定完整规格集合。"""
    stocks = wsc.get_stocks(session, store_id=warehouse_id, verbose=True, max_pages=max_pages)
    mapping = {}
    for s in stocks:
        art = s.get("article")
        chrt = positive_id(s.get("chrtId"))
        if not art or chrt is None or (s.get('storeId') is not None and positive_id(s['storeId']) != warehouse_id):
            raise ValueError('实时库存标识无效或含非莫斯科仓记录')
        key = (str(art), chrt)
        if key in mapping:
            raise ValueError('实时库存存在重复规格记录')
        mapping[key] = quantity(s.get('amount'))
    return mapping


def build_plans(vcs, shops, state, amount, resolve="snapshot", max_pages=200, *, shop_rows=None):
    """逐店组装改库存计划。resolve 见模块 docstring。产物结构与 ops_plan.plan_stock 一致。"""
    if resolve not in {'snapshot', 'live'} or type(max_pages) is not int or max_pages < 1:
        raise ValueError('库存解析模式或分页上限无效')
    with ProductDataResolver() as resolver:
        plans = plan_stock(vcs, shops, state, amount, resolver=resolver, shop_rows=shop_rows, report=resolve == "snapshot")
    if resolve == 'snapshot':
        return plans

    # live仅补充当前库存；不能用库存缺行排除已确认的商品规格。
    cred = credentials.get()
    for plan in plans:
        sid = plan['shopId']
        if not plan['items']:
            continue
        shop = cred.wb_shop(sid)
        session = None
        try:
            if not shop:
                raise ValueError('缺少WB网页凭证')
            wh = plan['warehouses'][0]['warehouseId']
            session = wb_api.make_session(shop, cred.root_version)
            mapping = _live_stock_map(session, max_pages, wh)
        except Exception as error:
            reason = ('缺少WB网页凭证' if not shop else f'莫斯科实时库存读取失败（{type(error).__name__}）')
            targets = {item['vc']: item for item in plan['items']}
            for vc, item in targets.items():
                plan.setdefault('blocked_items', []).append({'vc': vc, 'cn': item.get('cn', ''), 'reason': reason})
            plan['items'], plan['warehouses'] = [], []
        else:
            for item in plan['items']:
                current = mapping.get((item['vc'], item['chrtId']))
                item["cur_amt"] = current
        finally:
            if session is not None:
                session.close()
    report_plan_skips(plans, "设库存（实时）")
    return plans



def _stock_batches(session, warehouse_id, chrt_items, chunk, interval):
    for offset in range(0, len(chrt_items), chunk):
        batch = chrt_items[offset:offset + chunk]
        try:
            reply = wsc.post_stocks(session, warehouse_id, batch)
            flag = reply.get("error") if isinstance(reply, dict) else None
            if flag is False and not reply.get("additionalErrors"):
                status, message = "accepted", ""
            elif flag is True:
                status, message = "failed", str(reply.get("errorText") or "WB 拒绝库存提交")
            else:
                status, message = "unknown", "WB 库存回执缺少明确受理状态"
            data = reply.get("data") if isinstance(reply, dict) else None
            task_id = data.get("taskId") if isinstance(data, dict) else None
            if type(task_id) not in (str, int):
                task_id = None
        except AuthenticationError:
            raise
        except Exception as error:
            status, message, task_id = "unknown", f"WB 库存回执不可用（{type(error).__name__}）", None
        yield batch, status, message, task_id
        if offset + len(batch) < len(chrt_items):
            time.sleep(interval)


def run(args) -> int:
    common.validate_batch_options(chunk=getattr(args, "chunk", None), interval=getattr(args, "interval", None))
    if getattr(args, "max_pages", 200) < 1:
        raise ValueError("库存分页上限必须大于零")
    return ProductResult("stock", "wb", args).run(_run, args)


def _run(args, result: ProductResult) -> None:
    plans = getattr(args, 'frozen', None)
    if plans is None:
        shops = result.shops = selected_shops(args)
        state, shop_rows, book = load_state(shops, all_products=getattr(args, "all_products", False))
        vcs, desc = resolve_filters(args, state, book)
        print(f"目标：{desc}，通道 WB 后台")
        plans = build_plans(vcs, shops, state, args.amount, resolve=getattr(args, "resolve", "snapshot"),
                            max_pages=getattr(args, "max_pages", 200), shop_rows=shop_rows)
        args.prepared = plans
    result.add_plans(plans)
    if not result.apply:
        dry_run(plans, "stock")
        return
    if not any(plan["items"] for plan in plans):
        return
    if not confirm_irreversible("stock", args.amount, getattr(args, "yes", False)):
        result.cancelled = True
        print("已取消，未执行任何操作")
        return

    cred = credentials.get()
    chunk = min(getattr(args, "chunk", STOCK_CHUNK), wsc.MAX_CHUNK)
    interval = getattr(args, "interval", BATCH_SLEEP)
    for plan in plans:
        if not plan["items"]:
            continue
        sid = plan["shopId"]
        stock = plan["warehouses"][0]
        by_spec = {it["chrtId"]: it for it in plan["items"]}
        session = None
        try:
            shop = cred.wb_shop(sid)
            if not shop:
                raise AuthenticationError("无 WB 凭证")
            session = wb_api.make_session(shop, cred.root_version)
            for batch, status, message, task in _stock_batches(session, stock["warehouseId"],
                                                               stock["stockItems"], chunk, interval):
                for row in batch:
                    result.record(sid, by_spec.pop(row["chrtId"]), status, message, task)
        except Exception as error:
            message = str(error) if isinstance(error, AuthenticationError) else f"WB 会话不可用（{type(error).__name__}）"
            for remaining in by_spec.values():
                result.record(sid, remaining, "blocked", message)
        finally:
            if session is not None:
                session.close()
        time.sleep(SHOP_SLEEP)
    common.print_write_hint()


def set_stock(shop, warehouse_id, chrt_items, *, root_version=None,
              chunk=STOCK_CHUNK, interval=BATCH_SLEEP) -> dict:
    """下架清零用的 WB 单仓提交，保留规格级回执；鉴权失败交由下架阶段中止本店。"""
    common.validate_batch_options(chunk=chunk, interval=interval)
    session = wb_api.make_session(shop, root_version)
    details = []
    try:
        for batch, status, msg, task_id in _stock_batches(session, warehouse_id, chrt_items,
                                                          min(chunk, wsc.MAX_CHUNK), interval):
            details.extend({**item, "ok": status == "accepted", "status": status,
                            "msg": msg or ("OK" if status == "accepted" else "平台未确认"),
                            "taskId": task_id} for item in batch)
    finally:
        if hasattr(session, "close"):
            session.close()
    ok = sum(item["ok"] for item in details)
    return {"ok": ok, "fail": len(details) - ok, "details": details}

"""WB 后台折扣计划与提交。"""
import csv
from datetime import datetime
from pathlib import Path

from sellerops.core.errors import AuthenticationError
from sellerops.features.catalog.selection import load_state, resolve_filters
from sellerops.features.products.discount_products import targeted_discount_plan
from sellerops.features.products.plans import report_plan_skips
from sellerops.features.products.prices import submit_wb_prices
from sellerops.features.products.product_data import ProductDataResolver
from sellerops.features.products.results import ProductResult
from sellerops.integrations.wildberries.client import WBClient
from sellerops.persistence import db
from sellerops.settings import credentials, paths as config


def _storewide_plan(sid: int, client: WBClient, args, resolve_cn, entries: dict) -> dict:
    target, limit = args.target, args.limit
    threshold = args.threshold if args.threshold is not None else -1
    below = getattr(args, "below", -1)
    goods = []
    if threshold >= 0 or below < 0:
        goods = client.fetch_discount_goods_desc(threshold, limit=limit, skip_discount=target)
    remaining = limit - sum(row["discount"] != target for row in goods) if limit else 0
    if below >= 0 and (not limit or remaining > 0):
        seen = {row["nmID"] for row in goods}
        goods.extend(client.fetch_discount_goods_asc(below, limit=remaining, skip_discount=target,
                                                     seen_ids=seen))
    plan = {"shopId": sid, "items": [], "blocked_items": [], "skipped_items": []}
    seen = set()
    for row in goods:
        if row["nmID"] in seen:
            continue
        seen.add(row["nmID"])
        vc = row["vendorCode"]
        item = {"nmID": row["nmID"], "vc": vc, "cn": resolve_cn(vc, ""),
                "old_discount": row["discount"], "discount": target,
                "currencyIsoCode": row.get("isoCode4217") or "CNY"}
        if db.resolve_vc(vc, entries)['status'] == 'excluded':
            plan['skipped_items'].append({**item, 'reason': '\u8be5\u5546\u54c1\u5df2\u6807\u8bb0\u4e3a\u975e\u8d27\u76d8'})
        elif row["discount"] == target:
            plan["skipped_items"].append({**item, "reason": "平台当前折扣已等于目标"})
        else:
            plan["items"].append(item)
    return plan


def _write_csv(result: ProductResult, names: dict[int, str]) -> None:
    rows = []
    for sid in result.shops:
        receipts = result.receipts[sid]
        for r in receipts:
            item = r.item
            rows.append([sid, names.get(sid, ""), item.get("nmID"), item.get("vc"), item.get("cn", ""),
                         item.get("old_discount"), result.targets["discount"], r.status, r.task_id, r.message])
    if not rows:
        return
    output = Path(config.current.log_dir) / f"折扣修改_wb_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["shop_id", "shop_name", "nmID", "vendorCode", "cn", "old_discount", "new_discount",
                         "status", "taskId", "error"])
        writer.writerows(rows)
    result.artifacts["discount_csv"] = output


def run_cli(args) -> int:
    return ProductResult("discount", "wb", args).run(_run, args)


def _run(args, result: ProductResult) -> None:
    from sellerops.persistence.mappings import MappingRepository

    cred = credentials.get()
    plans = getattr(args, 'frozen', None)
    if plans is None:
        shops = result.shops or list(dict.fromkeys(int(shop["shopId"]) for shop in cred.shops if shop.get("shopId")))
        result.shops = shops
        if not shops:
            raise ValueError("没有配置 WB 目标店铺")
        direct = not getattr(args, "all_products", False)
        if direct:
            state, shop_rows, book = load_state(shops)
            vcs, desc = resolve_filters(args, state, book)
            print(f"商品范围：{desc}，目标折扣 {args.target}%")
        else:
            resolve_cn, _ = MappingRepository.build_vc_resolver()
            entries = db.vc_entries()
        plans, shop_config = [], {}
        with ProductDataResolver() as resolver:
            for sid in shops:
                client = None
                try:
                    shop = cred.wb_shop(sid)
                    if not shop:
                        raise AuthenticationError("缺少 WB 网页凭证，请运行 sellerops login wb")
                    shop_config[sid] = shop
                    if direct:
                        plan = targeted_discount_plan(sid, vcs, shop_rows[sid], state, args, resolver)
                    else:
                        client = WBClient(shop, cred.root_version)
                        plan = _storewide_plan(sid, client, args, resolve_cn, entries)
                except Exception as error:
                    reason = str(error) if isinstance(error, AuthenticationError) else f"本店折扣范围读取不完整：{error}"
                    plan = {"shopId": sid, "items": [], "blocked_items": [], "skipped_items": [], "range_error": reason}
                finally:
                    if client is not None:
                        client.session.close()
                plans.append(plan)
        args.prepared = plans
    else:
        shop_config = {plan['shopId']: cred.wb_shop(plan['shopId']) for plan in plans}
    report_plan_skips(plans, "改折扣")
    result.add_plans(plans)
    names = {sid: (shop or {}).get("shopName", "") for sid, shop in shop_config.items()}
    for plan in plans:
        sid = plan["shopId"]
        if not result.apply:
            for item in plan["items"]:
                print(f"  店{sid} {item['vc']}：折扣 {item['old_discount']} → {args.target}%")
            continue
        if not plan["items"]:
            continue
        try:
            client = WBClient(shop_config[sid], cred.root_version)
        except Exception as error:
            for item in plan["items"]:
                result.record(sid, item, "blocked", f"WB 会话不可用（{type(error).__name__}）")
            continue
        try:
            submit_wb_prices(client, plan, args.chunk or 100, 0.3, result)
        finally:
            client.session.close()
    _write_csv(result, names)

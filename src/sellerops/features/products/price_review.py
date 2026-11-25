"""
sellerops 价格审核「应用新价格」（原「获取价格审核列表、审核价格」抓包）

改价降价 30-49.9% 会进入 WB 价格审查（隔离区 quarantine），需调本模块「应用新价格」才能生效。
流程：GET quarantine/goods 拉待审列表（limit/offset 翻页）→ POST 同路径提交审核（body {"data":[id,...]}）。
鉴权：WB cookie 会话三件套（wb_api.py）。
"""
from contextlib import closing
import csv
from datetime import datetime
import time

from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.core import utils as common
from sellerops.core.errors import AuthenticationError, PlatformApiError
from sellerops.domain.snapshot_values import positive_id
from sellerops.features.products.plans import price_review_items
from sellerops.persistence.price_book import to_number
from sellerops.integrations.wildberries import client as wb_api

QUARANTINE = "https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/quarantine/goods"
PAGE_SIZE = 100


class QuarantineListError(ValueError):
    """待审列表校验失败，消息只包含本地生成的原因。"""


def fetch_quarantine(session, limit=PAGE_SIZE, offset=0):
    """GET 待审商品列表（单页）。返回 items 列表。"""
    reply = wb_api.request(session, "GET", f"{QUARANTINE}?limit={limit}&offset={offset}")
    if not isinstance(reply, dict):
        raise QuarantineListError("WB 价格审核清单回执不完整")
    flag, message = reply.get("error"), reply.get("errorText", "")
    additional = reply.get("additionalErrors")
    if flag is True or (isinstance(message, str) and message) or additional not in (None, {}, ""):
        raise QuarantineListError("WB 价格审核清单返回业务错误")
    data = reply.get("data")
    if flag is not False or message != "" or not isinstance(data, dict) or "quarantineGoods" not in data:
        raise QuarantineListError("WB 价格审核清单回执不完整")
    items = data["quarantineGoods"]
    if items is None:
        return []
    if not isinstance(items, list):
        raise QuarantineListError("WB 价格审核清单回执不完整")
    return items


def fetch_all_quarantine(session, limit=PAGE_SIZE, max_pages=200):
    """读取完整待审列表并拒绝重复页。"""
    if type(limit) is not int or limit <= 0 or type(max_pages) is not int or max_pages <= 0:
        raise ValueError("价格审核分页参数必须为正整数")
    all_items, seen = [], set()
    for page in range(max_pages):
        items = fetch_quarantine(session, limit, page * limit)
        if len(items) > limit:
            raise QuarantineListError("WB 价格审核清单页超过请求条数")
        for item in items:
            review_id = positive_id(item.get("id")) if isinstance(item, dict) else None
            if review_id is None or review_id in seen:
                raise QuarantineListError("WB 价格审核清单含无效或重复 ID")
            seen.add(review_id)
        all_items.extend(items)
        if len(items) < limit:
            return all_items
    raise QuarantineListError("WB 价格审核清单超过安全分页上限")


def apply_prices(session, ids):
    """POST 审核：body {"data": ids}。返回响应 dict。"""
    return wb_api.request(session, "POST", QUARANTINE, json={"data": ids})


def _read_failure(error):
    if isinstance(error, AuthenticationError):
        return "WB 会话失效或权限不足"
    if isinstance(error, QuarantineListError):
        return str(error)
    if isinstance(error, PlatformApiError) and type(error.status_code) is int:
        return f"WB 价格审核清单读取失败（HTTP {error.status_code}）"
    return f"WB 价格审核清单读取失败（{type(error).__name__}）"


def review_submitted_prices(sid: int, accepted: list[dict], result, *, stopped: bool = False) -> None:
    """单独记录本轮已受理改价对应的审核结果。"""
    items = price_review_items(accepted)
    if not items:
        return
    session = None
    ready, ids = [], []
    try:
        shop = credentials.get().wb_shop(sid)
        if stopped or not shop:
            raise AuthenticationError("WB 会话不可用，未提交价格审核")
        session = wb_api.make_session(shop)
        quarantine = fetch_all_quarantine(session)
        pending = {}
        for row in quarantine:
            nm_id = positive_id(row.get("nmID"))
            if nm_id is not None:
                pending.setdefault(nm_id, []).append(row)
        for item in items:
            candidates = [row for row in pending.get(item["nmID"], [])
                          if to_number(row.get("newPrice")) == item["price"]]
            qid = positive_id(candidates[0].get("id")) if len(candidates) == 1 else None
            if qid is None:
                reason = "本次目标价尚未出现在待审列表" if not quarantine else "待审列表尚无唯一匹配的本次目标价"
                result.record(sid, item, "blocked", reason, review=True)
            else:
                ready.append(item)
                ids.append(qid)
    except Exception as error:
        for item in items:
            result.record(sid, item, "blocked", f"价格审核准备失败：{_read_failure(error)}", review=True)
        if session is not None:
            session.close()
        return
    if not ready:
        session.close()
        return
    try:
        if len(set(ids)) != len(ids):
            status, message = "blocked", "待审核记录 ID 重复"
        else:
            reply = apply_prices(session, ids)
            flag = reply.get("error") if isinstance(reply, dict) else None
            status = "accepted" if flag is False else "failed" if flag is True else "unknown"
            message = {"accepted": "", "failed": "WB 拒绝价格审核", "unknown": "WB 价格审核回执状态未知"}[status]
    except AuthenticationError:
        status, message = "blocked", "WB 会话失效或权限不足"
    except Exception as error:
        status, message = "unknown", f"价格审核回执不可用（{type(error).__name__}）"
    finally:
        session.close()
    for item in ready:
        result.record(sid, item, status, message, review=True)


def process_shop(shop, root_version, args, rows, *, frozen=None):
    name = shop.get("shopName", shop.get("shopId"))
    print(f"\n=== 店铺 {name} (id={shop['shopId']}) ===")
    with closing(wb_api.make_session(shop, root_version)) as session:
        return _process_shop(session, shop, name, args, rows, frozen)


def _process_shop(session, shop, name, args, rows, frozen):
    st = {"total": 0, "applied": 0, "failed": 0}
    items = fetch_all_quarantine(session) if frozen is None else frozen
    st["total"] = len(items)
    print(f"  [待审列表] {len(items)} 个商品待审核")
    if not items:
        return st
    if frozen is None and args.limit > 0:
        items = items[:args.limit]
        print(f"  [截断] 按 --limit 仅处理前 {len(items)} 个")

    for it in items:
        rows.append({
            "店铺": name, "shopId": shop["shopId"],
            "id": it.get("id"), "nmID": it.get("nmID"),
            "vendorCode": it.get("vendorCode"), "title": it.get("title"),
            "oldPrice": it.get("oldPrice"), "newPrice": it.get("newPrice"),
            "priceDiffPercent": it.get("priceDiffPercent"),
            "结果": "DRY" if not args.apply else "待提交",
        })
        print(f"    id={it.get('id')} nmID={it.get('nmID')} {it.get('title')} "
              f"{it.get('oldPrice')}→{it.get('newPrice')} ({it.get('priceDiffPercent')}%)")

    if args.apply:
        ids = [it.get("id") for it in items if it.get("id") is not None]
        if not ids:
            return st
        try:
            d = apply_prices(session, ids)
            ok = d.get("error") is False
            st['unknown'] = type(d.get('error')) is not bool
            if ok:
                st["applied"] = len(ids)
                for row in rows[-len(items):]:
                    row["结果"] = "审核已受理"
            else:
                st["failed"] = len(ids)
                for row in rows[-len(items):]:
                    row["结果"] = "审核回执未知" if st["unknown"] else "WB 拒绝价格审核"
            print(f"    >> 审核提交 {'成功' if ok else rows[-1]['结果']} {len(ids)} 个")
        except Exception as e:
            st["failed"] = len(ids)
            st['unknown'] = True
            print(f'\u5ba1\u6838\u56de\u6267\u672a\u77e5\uff08{type(e).__name__}\uff09')
    return st


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    from sellerops.features.catalog.shops import archive_set
    archived = archive_set()
    frozen = {plan['shopId']: plan['items'] for plan in args.frozen} if hasattr(args, 'frozen') else None
    requested = ({int(x) for x in args.shops.split(",") if x.strip()} if args.shops else set())
    if frozen is not None:
        requested = set(frozen)
    all_shops = frozen is None and not args.shops
    shops = [shop for shop in cred.wb_shops()
             if shop['shopId'] not in archived and (all_shops or shop['shopId'] in requested)]
    failed_shops = [{'shop_id': sid, 'error': '店铺已归档' if sid in archived else '缺少本店 WB 会话'}
                    for sid in sorted(requested - {shop['shopId'] for shop in shops})]
    for failure in failed_shops:
        print(f"  [警告] 店铺 {failure['shop_id']}：{failure['error']}")
    if not shops:
        args.summary = "指定店铺无可用 WB 会话" if args.shops or requested else "没有可查询的 WB 店铺"
        print(f"[错误] {args.summary}")
    root_version = cred.root_version
    shop_names = [f"{s['shopName']}({s['shopId']})" for s in shops]
    print(f"店铺 {len(shops)} 个: {shop_names}{'' if args.apply else '  [dry-run]'}")

    rows = []
    totals = {"total": 0, "applied": 0, "failed": 0}
    unknown = False
    for shop in shops:
        try:
            st = process_shop(shop, root_version, args, rows,
                              frozen=None if frozen is None else frozen[shop['shopId']])
            unknown |= st.get('unknown', False)
            for k in totals:
                totals[k] += st[k]
        except Exception as error:
            reason = _read_failure(error)
            failed_shops.append({'shop_id': shop['shopId'], 'error': reason})
            print(f"  [警告] 店铺 {shop['shopName']}：{reason}（该店中止）")
        time.sleep(0.5)

    print(f"\n[汇总] 待审 {totals['total']} | "
          f"{'已应用新价格 ' + str(totals['applied']) if args.apply else '预览 ' + str(len(rows))}"
          f" | 失败 {totals['failed']} | 店铺异常 {len(failed_shops)}")
    if rows:
        now = datetime.now()
        path = config.daily_log_dir(now) / f"价格审核_{now:%Y%m%d_%H%M%S}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "shopId", "id", "nmID", "vendorCode",
                                              "title", "oldPrice", "newPrice", "priceDiffPercent", "结果"])
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")
    if not args.apply:
        args.prepared = [{'shopId': sid, 'items': [row for row in rows if row['shopId'] == sid]}
                         for sid in sorted({row['shopId'] for row in rows})]
        if not rows and shops:
            args.summary = "待审核清单读取不完整" if failed_shops else "所选店铺无待审核商品"
            print(f"[{'警告' if failed_shops else '完成'}] {args.summary}")
    args.outcome = {'items': [{'status': 'unknown'}] if unknown else [],
                    'failed_shops': failed_shops, **totals}
    return int(bool(totals["failed"] or failed_shops or not shops))

"""按商品计划分批提交 WB 价格，记录两阶段受理结果。"""
import time
from datetime import datetime
from sellerops.features.catalog.selection import load_state, selected_shops, resolve_filters
from sellerops.features.products.plans import plan_price, price_limit_violations, dry_run, RED, GREEN, YELLOW, RESET

from sellerops.integrations.wildberries import client as wb_api
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.features.products.product_data import ProductDataResolver
from sellerops.persistence.product_results import write_product_receipts
from sellerops.features.products.execution import _auto_review_shop

PRICE_CHUNK = 100   # upload/task 单批条数（对齐 discount 命令默认 100）
MAX_CHUNK = 300     # chunk 夹取上限（discount help 实践：最大 300）
BATCH_SLEEP = 0.3
SHOP_SLEEP = 0.6
ACTION_TAG = "price_wb"   # ops_result.csv 操作列标签（与 BCS 通道的 "price" 区分）


def _build_payload(item, discount):
    """plan_price item → dp-api upload/task 条目。

    只改价：{"vendorCode","nmID","price","currencyIsoCode"}（HAR 实证无 discount 字段）；
    --discount 显式给出（含 0）时附加 discount 字段。
    """
    d = {"vendorCode": item["vc"], "nmID": int(item["nmID"]), "currencyIsoCode": "CNY"}
    if item['price'] is not None:
        d['price'] = int(item['price'])
    if discount is not None:
        d["discount"] = int(discount)
    return d


def exec_shop(client, plan, args, results, ts):
    """单店提交：≤原价/2 预拦截 → 分批 upload/task（预检后自动确认）。返回 (ok, fail)。"""
    sid = plan["shopId"]
    ok = fail = 0
    # ① ≤原价/2 预拦截（dry-run 阶段已黄标提示，apply 时剔除）
    bad = price_limit_violations(plan["items"])
    fail += len(bad)
    ok_items = plan["items"]
    if bad:
        print(f"  {RED}[跳过] {len(bad)} 条低于原价一半（WB quarantine 硬限）：{RESET}")
        for vc, cn, cur, target in bad:
            print(f"    {vc} | {cn} | 原价 {cur} → 目标 {target}")
            results.append([sid, vc, ACTION_TAG, target, 'SKIP: 低于原价一半', ts])
        ok_items = [it for it in plan["items"]
                    if not any(it["vc"] == b[0] and it["price"] == b[3] for b in bad)]
    if not ok_items:
        print("  本店无有效改价项，跳过")
        return 0, fail

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
        except common.CookieExpiredError as error:
            pending = ok_items[i:]
            results.extend([sid, it["nmID"], ACTION_TAG, it["price"], f"鉴权中止：{error}", ts] for it in pending)
            return ok, fail + len(pending)
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
            results.append([sid, it["nmID"], ACTION_TAG, it["price"], msg if ok_flag else f"FAIL: {msg}", ts])
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


def run(args):
    common.validate_batch_options(chunk=getattr(args, "chunk", None),
                                  interval=getattr(args, "interval", None))
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

    shops = selected_shops(args)
    state, shop_rows, boss = load_state(shops, all_products=getattr(args, "all_products", False))
    vcs, desc = resolve_filters(args, state, boss)
    if not vcs:
        print("[提示] 筛选结果为空，无操作")
        return 0
    print(f"目标: {desc}（{len(vcs)} 个 vendorCode）· 通道=WB 原生 dp-api（预检后自动确认降价/隔离区弹窗）")
    with ProductDataResolver() as resolver:
        plans = plan_price(vcs, shops, state, boss, getattr(args, "price", None),
                           discount, club, keep_price=getattr(args, "keep_price", False),
                           resolver=resolver, allow_discount_only=True, shop_rows=shop_rows)
    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法操作（上方 [跳过] / [跳过汇总] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / 无目标价。")
        return 0

    if not getattr(args, "apply", False):
        dry_run(plans, "price")
        return 1 if any(p.get('blocked_items') for p in plans) else 0

    cred = credentials.get()
    ok = fail = 0
    results = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pi, p in enumerate(plans):
        sid = p["shopId"]
        for item in p.get('blocked_items', []):
            fail += 1
            print(f"  [已阻止] 店{sid} {item['vc']}: {item['reason']}")
            results.append([sid, item['vc'], ACTION_TAG, '', f"SKIP: {item['reason']}", ts])
        if not p['items']:
            continue
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
            if getattr(args, "auto_review", False) and o and not f:
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

    write_product_receipts(config.current, plans, results)
    print(f"\n结果: 已受理 {ok} · 失败或未确认 {fail}（明细已保存 {config.current.result_csv}）")
    _report_zero_items(plans)

    common.print_write_hint()
    return 1 if fail else 0



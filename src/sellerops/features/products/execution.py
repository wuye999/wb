"""按店铺分批提交商品操作，记录受理、失败和结果未知的明细。"""
from sellerops.persistence.product_results import write_product_receipts
import time
from datetime import datetime

from sellerops.integrations.bcs.client import client as bcs
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.features.products.plans import price_limit_violations
from sellerops.features.products.plans import price_review_items
from sellerops.features.products.plans import PRICE_HALF_LIMIT_NOTE
from sellerops.features.products.trash_execution import clear_stock_for_trash

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

PRICE_CHUNK = 300
STOCK_CHUNK = 200
BATCH_SLEEP = 0.15
SHOP_SLEEP = 0.6
TRASH_CHUNK = 300        # 移回收站单批 nmID 上限（项目硬性 ≤300/批，WB/BCS 通用）
TRASH_INTERVAL = 0.3     # 移回收站批间间隔（BCS 有 19s 限流窗口，用 --interval 21 规避）


def _auto_review_shop(sid, items):
    """仅审核本次受理且需要价格审核的商品。"""
    review_items = price_review_items(items)
    if not review_items:
        return 0
    shop = credentials.get().wb_shop(sid)
    if not shop:
        print(f"  [自动审核] 店铺 {sid} 无 WB 凭证，跳过")
        return 0
    from sellerops.features.products.discounts import Discounts
    try:
        r = Discounts().apply_new_prices_by_nmids(shop, [it.get("nmID") for it in review_items])
        print(f"  [自动审核] {r['note']}")
        return r["applied"]
    except common.CookieExpiredError as e:
        print(f"  [自动审核] 店铺 {sid} cookie 失效，跳过: {e}")
        return 0
    except Exception as e:
        print(f"  [自动审核] 店铺 {sid} 失败: {e}")
        return 0


def _submit(label, method, *args):
    """执行一批 BCS 写入，并保留本批回执。"""
    try:
        r = method(*args)
        msg = r.get("msg", "")
        if r.get("code") == 200:
            print(f"  {GREEN}✓{RESET} {label} 成功: {msg}")
            return True, "已受理" + (f"：{msg}" if msg else "")
        print(f"  {RED}✗{RESET} {label} 失败: code={r.get('code')} {msg}")
        return False, f"FAIL: {msg}"
    except Exception as e:
        print(f"  {RED}✗{RESET} {label} 异常: {e}")
        return False, f"结果未知：{e}"


def run_apply(plans, action, auto_review=False, *, channel="bcs", chunk=None, interval=None):
    """按计划分批提交，清零已受理的商品才能移入回收站。"""
    common.validate_batch_options(chunk=chunk, interval=interval)
    ok = fail = 0
    skipped_bad = 0
    zero_items = []
    results = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pi, p in enumerate(plans):
        sid = p["shopId"]
        print(f"\n>>> 店{sid}：{action}")
        if action == "price":
            for item in p.get('blocked_items', []):
                fail += 1
                print(f"  [已阻止] 店{sid} {item['vc']}: {item['reason']}")
                results.append([sid, item['vc'], action, '', f"SKIP: {item['reason']}", ts])
            bad = price_limit_violations(p["items"])
            if bad:
                print(f"  {RED}[跳过] {len(bad)} 条{PRICE_HALF_LIMIT_NOTE}：{RESET}")
                for vc, cn, cur, target in bad:
                    print(f"    {vc} | {cn} | 原价 {cur} → 目标 {target}")
                    results.append([sid, vc, action, target, 'SKIP: 低于原价一半', ts])
                skipped_bad += len(bad)
                fail += len(bad)
            ok_items = [it for it in p["items"] if not any(it["vc"] == b[0] and it["price"] == b[3] for b in bad)]
            zero_items.extend((sid, it["vc"], it.get("cn", ""), it.get("cur_price"), it["price"])
                              for it in ok_items if it.get("orig_zero"))
            dl = [{"nmID": it["nmID"], "price": it["price"],
                   "discount": it["discount"], "clubDiscount": it["clubDiscount"]}
                  for it in ok_items]
            if not dl:
                print("  本店无有效改价项，跳过")
                continue
            accepted_prices = []
            for i in range(0, len(dl), PRICE_CHUNK):
                batch = dl[i:i + PRICE_CHUNK]
                ok_flag, msg = _submit(f"{action} 批{i // PRICE_CHUNK + 1}", bcs.batch_set_prices, sid, batch)
                if ok_flag:
                    ok += len(batch)
                    accepted_prices.extend(ok_items[i:i + PRICE_CHUNK])
                else:
                    fail += len(batch)
                for it in batch:
                    results.append([sid, it["nmID"], action, it["price"], msg, ts])
                if i + PRICE_CHUNK < len(dl):
                    time.sleep(BATCH_SLEEP)
            if auto_review and accepted_prices:
                _auto_review_shop(sid, accepted_prices)
        elif action == "stock":
            for it in p.get("blocked_items", []):
                fail += 1
                print(f"  [已阻止] 店{sid} {it['vc']}: {it['reason']}")
                results.append([sid, it['vc'], action, "", f"SKIP: {it['reason']}", ts])
            for wh in p["warehouses"]:
                sis = wh["stockItems"]
                for i in range(0, len(sis), STOCK_CHUNK):
                    batch = sis[i:i + STOCK_CHUNK]
                    ok_flag, msg = _submit(f"stock 仓库{wh['warehouseId']} 批{i // STOCK_CHUNK + 1}",
                                           bcs.batch_set_stock, sid, wh["warehouseId"], batch)
                    if ok_flag:
                        ok += len(batch)
                    else:
                        fail += len(batch)
                    for it in batch:
                        results.append([sid, it["chrtId"], action, it["amount"], msg, ts])
                    if i + STOCK_CHUNK < len(sis):
                        time.sleep(BATCH_SLEEP)
            zero_items.extend((sid, it["vc"], it.get("cn", ""), it.get("cur_amt"), it["amount"])
                              for it in p["items"] if it.get("orig_zero"))
        elif action == "trash":
            tag = "trash_wb" if channel == "wb" else "trash_bcs"   # CSV「操作」列区分通道
            ready, blocked = clear_stock_for_trash(sid, p["items"])
            blocked = [*p.get("blocked_items", []), *blocked]
            fail += len(blocked)
            for it in blocked:
                print(f"  [已阻止] 店{sid} {it.get('vc', '')}: {it['reason']}")
                results.append([sid, it.get("nmID") or it.get("vc"), tag, "", f"SKIP: {it['reason']}", ts])
            nms = list(dict.fromkeys(it["nmID"] for it in ready))
            if not nms:
                continue
            step = int(chunk or TRASH_CHUNK)
            gap = TRASH_INTERVAL if interval is None else float(interval)
            if channel == "wb":
                # 懒加载：与 stock_wb 同风格，避免把 wb_client 拖进所有 ops 命令的启动路径
                from sellerops.features.products import trash as trash_wb_mod
                shop = credentials.get().wb_shop(sid)
                if not shop:
                    print(f"  {RED}✗ 错误{RESET} 店{sid} 无 WB 凭证，本条通道无法下架"
                          f"（{len(nms)} 个 nmID 计入失败）")
                    print(f"    {RED}处置：先补该店 WB 凭证（sellerops login wb）后重跑 trash。"
                          f"本次使用 WB 通道。{RESET}")
                    fail += len(nms)
                    results.extend([sid, nm, tag, "", "SKIP: 无 WB 凭证", ts] for nm in nms)
                else:
                    r = trash_wb_mod.move_nms(shop, nms,
                                              chunk=step,
                                              interval=gap)
                    ok += r["ok"]
                    fail += r["fail"]
                    for d in r["details"]:
                        results.append([sid, d["nmID"], tag, "",
                                        "OK" if d["ok"] else f"FAIL: {d['msg']}", ts])
                    bad = [d for d in r["details"] if not d["ok"]]
                    if bad:
                        for d in bad[:10]:
                            print(f"    [失败] nmID={d['nmID']}: {d['msg']}")
                        if len(bad) > 10:
                            print(f"    ... 共 {len(bad)} 条")
                        if not r.get("aborted"):
                            print("  [提示] 若为「有库存/在途」原因，WB 会拒收：先归零库存后重跑")
                    print(f"  [移回收站·WB原生] 成功 {r['ok']} · 失败 {r['fail']}"
                          f"（{len(nms)} 个 nmID，{step}/批）")
                    if r.get("aborted"):
                        print(f"  {RED}✗ 错误{RESET} 店{sid} 移回收站中止：{r['aborted']}")
                        print(f"    {RED}本店剩余 nmID 已计入失败。请修好 WB 会话"
                              f"（sellerops login wb）后重跑 trash；"
                              f"不会自动改走 BCS。{RESET}")
            else:
                for i in range(0, len(nms), step):
                    batch = nms[i:i + step]
                    ok_flag, msg = _submit(f"trash_bcs 批{i // step + 1}", bcs.remove_to_trash, sid, batch)
                    if ok_flag:
                        ok += len(batch)
                    else:
                        fail += len(batch)
                    for nm in batch:
                        results.append([sid, nm, tag, "", msg, ts])
                    if i + step < len(nms):
                        time.sleep(gap)
        if pi < len(plans) - 1:
            time.sleep(SHOP_SLEEP)

    write_product_receipts(config.current, plans, results)
    summary = f"结果: 成功 {ok} · 失败 {fail}"
    if action == "trash" and channel == "wb":
        summary += "（按商品卡计数）"
    if skipped_bad:
        summary += f" · {RED}价格下限剔除 {skipped_bad}{RESET}"
    print(f"\n{summary}（明细已保存 {config.current.result_csv}）")

    if action == "trash" and channel == "wb" and fail:
        # WB 通道失败：只报错，不自动改走 BCS（BCS 限流/成功率均不可靠，不能当兜底）
        print(f"{RED}★ WB 通道有 {fail} 条未成功下架 —— 请按上面的报错先修原因，再重跑 trash。"
              f"{RESET}")
        print(f"{RED}  常见原因：WB cookie/seller-lk 过期、商品有库存或在途。{RESET}")
        print(f"{RED}  本次失败项已记录在结果文件中。{RESET}")

    if zero_items:
        print(f"\n{RED}⚠ [0 值商品报告] {len(zero_items)} 项操作前价格/库存为 0（WB 官方数据延迟或商品受限）{RESET}")
        print("  已照常提交修改；若之后查询仍为 0，属 WB 官方原因（修改生效可能延迟），无需反复修改。")
        seen = {}
        for sid, vc, cn, old, new in zero_items:
            seen.setdefault(vc, {"cn": cn, "shops": [], "old": old, "new": new})["shops"].append(sid)
        for vc, d in sorted(seen.items()):
            print(f"    {vc} | {d['cn']} | 原值 {d['old']} → 目标 {d['new']} | 店 {','.join(map(str, d['shops']))}")
    return ok, fail

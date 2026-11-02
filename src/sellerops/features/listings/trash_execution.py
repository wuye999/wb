"""下架前清库存：只放行全部规格均已受理归零的商品，不读取远端商品或库存。"""
from collections import Counter
import time

from sellerops.core import utils as common
from sellerops.settings import credentials
from sellerops.features.listings.plans import trash_item_error


def clear_stock_for_trash(sid: int, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回允许移回收站的商品与被阻止商品；失败原因保存在 reason。"""
    valid, blocked = [], []
    for item in items:
        reason = trash_item_error(item)
        if reason:
            blocked.append({**item, "reason": reason})
        else:
            valid.append({**item, "nmID": int(item["nmID"]),
                          "stock_specs": sorted({(int(c), int(w)) for c, w in item["stock_specs"]})})
    if not valid:
        return [], blocked
    warehouses = {w for item in valid for _, w in item["stock_specs"]}
    if len(warehouses) != 1:
        return [], blocked + [{**it, "reason": "同店下架计划的目标仓库不一致"} for it in valid]
    shop = credentials.get().wb_shop(sid)
    if not shop:
        return [], blocked + [{**it, "reason": "无 WB 凭证，无法清库存"} for it in valid]

    # 延迟导入：stocks 的 CLI 用例依赖 operations/execution。
    from sellerops.features.listings import stocks
    warehouse = next(iter(warehouses))
    required = {c for it in valid for c, _ in it["stock_specs"]}
    confirmed = set()
    reasons = {}
    aborted = False
    # 每次底层调用仅提交一个批次，保留此前已接受的结果，不重放失败批次。
    ordered = sorted(required)
    for offset in range(0, len(ordered), stocks.STOCK_CHUNK):
        batch = ordered[offset:offset + stocks.STOCK_CHUNK]
        try:
            result = stocks.set_stock(shop, warehouse, [{"chrtId": c, "amount": 0} for c in batch],
                                      chunk=stocks.STOCK_CHUNK)
            details = result.get("details") or []
            counts = Counter(d.get("chrtId") for d in details if isinstance(d, dict))
            good = {d["chrtId"] for d in details if isinstance(d, dict) and d.get("ok") is True
                    and d.get("amount") == 0 and d.get("chrtId") in batch and counts[d["chrtId"]] == 1}
            ambiguous = (any(not isinstance(d, dict) or d.get("chrtId") not in batch for d in details)
                         or any(n > 1 for n in counts.values())
                         or result.get("ok") != len(good) or result.get("fail") != len(batch) - len(good))
            if ambiguous:
                good = set()
            confirmed.update(good)
            messages = {d['chrtId']: str(d.get('msg') or '清库存未受理')[:160]
                        for d in details if isinstance(d, dict) and d.get('chrtId') in batch}
            for c in set(batch) - good:
                reasons[c] = ("清库存受理回执缺失、重复或计数不一致" if ambiguous
                              else messages.get(c, "缺少该规格的清零受理回执"))
        except common.CookieExpiredError:
            aborted = True
            for c in set(ordered[offset:]) - confirmed:
                reasons[c] = "WB 凭证失效，该店操作中止"
            break
        except Exception as exc:
            for c in batch:
                reasons[c] = f"清库存异常：{type(exc).__name__}: {str(exc)[:160]}"
        print(f"  [清库存] 店{sid} 莫斯科仓库{warehouse}：本批受理 {len(confirmed.intersection(batch))}/{len(batch)}")
        if offset + len(batch) < len(ordered):
            time.sleep(stocks.BATCH_SLEEP)
    ready = []
    for item in valid:
        failed = sorted(c for c, _ in item["stock_specs"] if c not in confirmed)
        if failed or aborted:
            reason = (reasons.get(failed[0]) if failed else "WB 凭证失效，该店操作中止")
            if failed:
                reason = f"规格 {failed[0]}：{reason}"
            blocked.append({**item, "reason": f"{reason or '无法确认清零'}；已阻止移回收站"})
        else:
            ready.append(item)
    return ready, blocked

"""promotion rules。"""


from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from sellerops.core import utils as common


from sellerops.features.pricing.promotion_types import ACTIVE_STATUS_ID
from sellerops.features.pricing.promotion_types import DEFAULT_MIN_ORDERS
from sellerops.features.pricing.promotion_types import GROUP_LISTED
from sellerops.features.pricing.promotion_types import GROUP_UNLISTED
from sellerops.features.pricing.promotion_types import KNOWN_STATUS_IDS
from sellerops.features.pricing.promotion_types import VERDICT_CLOSE
from sellerops.features.pricing.promotion_types import VERDICT_PAUSED
from sellerops.features.pricing.promotion_types import VERDICT_UNKNOWN

def aggregate_orders(rows: Sequence[Dict[str, Any]], shop_labels: Any = None
                     ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Dict[str, Any]]], int]:
    """按供应商代码聚合飞书「订单登记」记录（**跨店合计** + 各店拆分）。

    返回 `(by_vc, by_vc_shop, no_vc)`：
      - `by_vc`      = `{vc: {'orders','qty','cn'}}` —— **合计口径**：一条记录只计一次
        （即使它的 `店铺` 多选含多个目标店，也不重复计数）
      - `by_vc_shop` = `{vc: {店铺短名: {'orders','qty'}}}` —— **各店拆分**：对每个含它的目标店各计一次
      - `no_vc`      = 无 `BCS编号` 的目标店记录数（同样一条只计一次）

    ⚠ 两个口径用途不同（合计用于判定「值不值得推广」，拆分只用于展示），不可混用。
    `shop_labels` 为空 → 用记录自身的 `店铺` 判定是否命中。
    """
    labels: Optional[Set[str]] = set(shop_labels) if shop_labels else None
    by_vc: Dict[str, Dict[str, Any]] = {}
    by_vc_shop: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(lambda: defaultdict(
        lambda: {"orders": 0, "qty": 0}))
    no_vc = 0
    for r in (rows or []):
        shops = set(r.get("shops") or [])
        hit = (shops & labels) if labels is not None else shops
        if not hit:
            continue
        vc = (r.get("vc") or "").strip()
        if not vc:
            no_vc += 1
            continue
        qty = r.get("qty") or 1
        it = by_vc.setdefault(vc, {"orders": 0, "qty": 0, "cn": ""})
        it["orders"] += 1                             # ★ 合计：一条记录只计一次
        it["qty"] += qty
        if not it["cn"] and r.get("cn"):
            it["cn"] = r["cn"]
        for s in hit:                                 # ★ 拆分：对每个含它的店各计一次
            e = by_vc_shop[vc][s]
            e["orders"] += 1
            e["qty"] += qty
    return by_vc, {vc: dict(d) for vc, d in by_vc_shop.items()}, no_vc

def shop_rows_by_vc(rows: Sequence[Dict[str, Any]], shop_label: str
                    ) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """单店视角 `{vc: {'orders','qty','cn'}}`（= `aggregate_orders` 以该店为唯一目标的拆分口径）"""
    if not shop_label:
        return {}, 0
    by_vc, _, no_vc = aggregate_orders(rows, {shop_label})
    return by_vc, no_vc

def shop_split_text(split: Dict[str, Dict[str, Any]]) -> str:
    """各店单数拆分 → '袁州1=2,袁州3=1'（按店名排序）"""
    return ",".join(f"{s}={split[s].get('orders', 0)}" for s in sorted(split or {}))

def sort_gap(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """正向排序：合计单数降序 → 同数 vc 升序 → 店铺ID 升序"""
    def key(x: Dict[str, Any]):
        vc = str(x.get("vc") or "")
        return (vc.startswith("("), -(x.get("orders") or 0), vc, common.to_int(x.get("shop_id")))
    return sorted(items or [], key=key)

def sort_waste(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """反向排序：合计单数升序（0 单最前）→ 店铺ID / 供应商代码 / nm 升序"""
    return sorted(items or [], key=lambda x: (x.get("orders") or 0, common.to_int(x.get("shop_id")),
                                              str(x.get("vc") or ""), common.to_int(x.get("nm"))))

def build_gap(targets: Sequence[Tuple[Dict[str, Any], int, str]],
              rows: Sequence[Dict[str, Any]],
              adverted_by_shop: Dict[int, Set[str]],
              snapshots: Dict[int, Optional[Dict[str, Dict[str, Any]]]],
              min_orders: int = DEFAULT_MIN_ORDERS, resolve_cn: Any = None,
              with_cn: bool = True) -> Dict[str, Any]:
    """正向（该推没推）：**合计单数 ≥ min_orders** 且该 vc 在某个目标店**未在推广** → 该店缺口（纯函数）。

    - 同一 vc 可能对多个目标店各产出一行（每个未推的店都是缺口）。
    - 按该店快照是否在架分「可直接补推 / 需先上架」；快照文件缺失（None）→ 归「需先上架」并标注。
    """
    labels = [name for _, _, name in targets]
    by_vc, by_vc_shop, no_vc = aggregate_orders(rows, set(labels))
    listed: List[Dict[str, Any]] = []
    unlisted: List[Dict[str, Any]] = []
    stats = {"候选vc数": 0, "已推广店位": 0, "可直接补推": 0, "需先上架": 0,
             "缺nmId": 0, "无BCS编号": no_vc, "候选店铺位": 0}

    for vc, info in sorted(by_vc.items()):
        if info["orders"] < min_orders:
            continue
        stats["候选vc数"] += 1
        cn = info["cn"] or (((resolve_cn(vc, "") or "") if resolve_cn else "") if with_cn else "")
        split = by_vc_shop.get(vc) or {}
        split_txt = shop_split_text(split)
        for _shop, sid, name in targets:
            if vc in (adverted_by_shop.get(sid) or set()):
                stats["已推广店位"] += 1
                continue
            stats["候选店铺位"] += 1
            base = {"shop_id": sid, "shop_name": name, "vc": vc, "cn": cn,
                    "orders": info["orders"], "qty": info["qty"],
                    "shop_orders": (split.get(name) or {}).get("orders", 0),
                    "shop_split": split_txt}
            snap = snapshots.get(sid)
            if snap is None:
                unlisted.append({**base, "nm_id": "", "group": GROUP_UNLISTED,
                                 "备注": "本店快照缺失，无法判断在架"})
            elif vc not in snap:
                unlisted.append({**base, "nm_id": "", "group": GROUP_UNLISTED,
                                 "备注": "本店无该 vc（已下架/未上架）"})
            else:
                row = snap.get(vc) or {}
                nm_id = common.to_int(row.get("nmId") or row.get("nmID"))
                note = "" if nm_id else "本店在架但缺 nmId（建议先 fetch）"
                if not nm_id:
                    stats["缺nmId"] += 1
                listed.append({**base, "nm_id": nm_id, "group": GROUP_LISTED, "备注": note})

    stats["可直接补推"] = len(listed)
    stats["需先上架"] = len(unlisted)
    return {"gap_listed": sort_gap(listed), "gap_unlisted": sort_gap(unlisted), "gap_stats": stats}

def build_waste(targets: Sequence[Tuple[Dict[str, Any], int, str]],
                rows: Sequence[Dict[str, Any]],
                adverted_products: Sequence[Dict[str, Any]],
                min_orders: int = DEFAULT_MIN_ORDERS, with_cn: bool = True) -> Dict[str, Any]:
    """反向（在推但该关）：**合计单数 < min_orders** 的 vc，其在目标店所有「在投」活动里的推广都建议关闭。

    `adverted_products` = 各目标店被推广商品明细**打平**（每条须含 `shop_id`/`shop_name`）。

    - 合计 ≥ min_orders → 卖得动，**不进**任何清单；
    - 合计 < min_orders 且活动 `status_id == 9`（在投）→ 「建议关闭」；
    - 合计 < min_orders 但活动非在投（如 11 暂停）→ 「已暂停无需操作」（仅计数）；
    - `vc` 为空（nm 本地真源未收录）→ 「无法判定」（不推断）。
    """
    labels = [name for _, _, name in targets]
    by_vc, by_vc_shop, no_vc = aggregate_orders(rows, set(labels))
    n_camp: Counter = Counter((common.to_int(p.get("shop_id")), common.to_int(p.get("nm")))
                              for p in (adverted_products or []))
    close: List[Dict[str, Any]] = []
    paused: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    unknown_status: List[Tuple[int, Any, Any]] = []

    for p in (adverted_products or []):
        sid = common.to_int(p.get("shop_id"))
        nm = common.to_int(p.get("nm"))
        vc = (p.get("vc") or "").strip()
        info = by_vc.get(vc) or {}
        split = by_vc_shop.get(vc) or {}
        base = {
            "shop_id": sid, "shop_name": p.get("shop_name") or "",
            "campaign_id": p.get("campaign_id"), "campaign_name": p.get("campaign_name") or "",
            "status_id": common.to_int(p.get("status_id")),
            "payment_model": p.get("payment_model") or "",
            "budget": p.get("budget") if p.get("budget") is not None else "",
            "nm": nm, "vc": vc, "cn": p.get("cn") or "", "ru": p.get("ru") or "",
            "orders": 0, "qty": 0,
            "shop_orders": (split.get(p.get("shop_name") or "") or {}).get("orders", 0),
            "shop_split": shop_split_text(split),
            "n_campaigns": n_camp[(sid, nm)],
        }
        if not vc:
            unknown.append({**base, "verdict": VERDICT_UNKNOWN,
                            "备注": "nm 本地真源未收录 → 无法关联销量，请人工判断"})
            continue
        total, qty = info.get("orders", 0), info.get("qty", 0)
        if total >= min_orders:
            continue                                    # 三店合计卖得动 → 不该关
        note = "三店合计 0 单" if total == 0 else f"三店合计 {total} 单（低于阈值）"
        item = {**base, "orders": total, "qty": qty,
                "cn": base["cn"] or info.get("cn", ""), "备注": note}
        if base["status_id"] == ACTIVE_STATUS_ID:        # ★ 只把「在投」列为建议关闭
            close.append({**item, "verdict": VERDICT_CLOSE})
        else:
            paused.append({**item, "verdict": VERDICT_PAUSED})
            if base["status_id"] not in KNOWN_STATUS_IDS:
                unknown_status.append((base["status_id"], sid, base["campaign_id"]))

    return {
        "waste_close": sort_waste(close),
        "waste_paused": sort_waste(paused),
        "waste_unknown": unknown,
        "waste_by_nm": waste_by_nm(close),
        "waste_by_campaign": waste_by_campaign(adverted_products, close),
        "waste_unknown_status": unknown_status,
        "waste_stats": {"被推广商品位": len(adverted_products or []), "建议关闭": len(close),
                        "已暂停无需操作": len(paused), "无法判定": len(unknown), "无BCS编号": no_vc},
    }

def waste_by_nm(close_items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """（店铺 × 商品 nm）级汇总：该 nm 在本店被几个活动推广 + 该 vc 三店合计单数与本店单数"""
    agg: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for it in (close_items or []):
        k = (common.to_int(it.get("shop_id")), common.to_int(it.get("nm")))
        e = agg.setdefault(k, {"shop_id": k[0], "shop_name": it.get("shop_name"), "nm": k[1],
                               "vc": it.get("vc"), "cn": it.get("cn"),
                               "orders": it.get("orders"), "shop_orders": it.get("shop_orders"),
                               "qty": it.get("qty"), "campaigns": []})
        e["campaigns"].append(it.get("campaign_id"))
    out = list(agg.values())
    for e in out:
        e["n_campaigns"] = len(e["campaigns"])
    out.sort(key=lambda x: (x.get("orders") or 0, common.to_int(x.get("shop_id")),
                            common.to_int(x.get("nm"))))
    return out

def waste_by_campaign(all_products: Sequence[Dict[str, Any]],
                      close_items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """（店铺 × 活动）级汇总：该活动被推广商品数 / 其中建议关闭数 / 是否「整个活动建议关闭」"""
    total: Counter = Counter()
    meta: Dict[Tuple[int, Any], Dict[str, Any]] = {}
    for p in (all_products or []):
        k = (common.to_int(p.get("shop_id")), p.get("campaign_id"))
        total[k] += 1
        meta.setdefault(k, p)
    close_n: Counter = Counter((common.to_int(it.get("shop_id")), it.get("campaign_id"))
                               for it in (close_items or []))
    out: List[Dict[str, Any]] = []
    for (sid, cid), n in total.items():
        m = meta.get((sid, cid)) or {}
        n_close = close_n.get((sid, cid), 0)
        out.append({
            "shop_id": sid, "shop_name": m.get("shop_name") or "",
            "campaign_id": cid, "campaign_name": m.get("campaign_name") or "",
            "status_id": common.to_int(m.get("status_id")),
            "payment_model": m.get("payment_model") or "",
            "budget": m.get("budget") if m.get("budget") is not None else "",
            "被推广商品数": n, "建议关闭数": n_close,
            "是否整活动建议关闭": "★ 整个活动建议关闭" if (n > 0 and n_close == n) else "",
        })
    out.sort(key=lambda x: (-x["建议关闭数"], common.to_int(x["shop_id"]),
                            common.to_int(x["campaign_id"])))
    return out

# -*- coding: utf-8 -*-
"""
wb_ops `order-stats` 的「按供应商代码(VC)」跨店聚合与控制台渲染（只读）

背景：同一商品在各店铺 **vendorCode 相同、nmId 不同**（2026-10-30 实测：300 个 vc 中 85 个跨店，
各带 2~6 个不同 nmId）⇒ 以 **vc 为键跨店合并**才能看清「这个商品到底退多少」，
`nm_ids` 保留该 vc 涉及的全部 WB商品码。

口径与 `order_stats.aggregate_by_cn` 完全一致（不另立规矩）：
- 订单数 = 明细行数（一行=一件，不按订单号去重）；
- 退货数 = `kind=="return"`；未完结 = `kind=="pending"`；未知状态计入订单数但不归类；
- `rate = 退货数/订单数`；`rate_done = 退货数/(订单数 − 未完结)`，分母 ≤0 → None（xlsx 写 "-"）；
- 排序 `(是否以 "(" 开头, -订单数, vc)` —— `(无供应商代码)` 桶恒置尾。

纯只读：不做任何写操作，不触发同步/合并。
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

NO_VC = "(无供应商代码)"      # 以 "(" 开头 ⇒ 排序恒置尾（对齐 NO_CN 惯例）


def _nm_sort_key(nm: str) -> Tuple[int, int, str]:
    """nmId 排序键：纯字典序会把 "999" 排在 "1001" 之后，故数字型按数值比。"""
    return (0, int(nm), "") if nm.isdigit() else (1, 0, nm)


def aggregate_by_vc(rows: Sequence[Dict[str, Any]],
                    cn_stock_sku: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """按「供应商代码(vendorCode)」跨店聚合。

    Args:
        rows: `order_stats.collect()` 的明细行（需含 vc / nm_id / cn / shop_label / kind|status）。
        cn_stock_sku: {商品中文名: 库存SKU}，用于补「库存SKU」列。

    Returns:
        每行 dict：`{rank, vc, cn, orders, returns, pending, unknown,
        nm_ids: list[str], shops: list[str], stock_sku, rate, rate_done}`。

    Notes:
        同一 vc 的 `cn` 取**首个非空**值（映射真源 vc→中文名实测 1:1，冲突时不告警）；
        `--no-cn` 场景下保持空串，**不**伪造成 `(中文名未知)`；vc 为空归 `NO_VC` 桶。
        `classify_status` 在函数体内 import —— 本模块被 `order_stats` 顶层 import，
        顶层反向 import 会构成循环依赖。
    """
    from .order_stats import classify_status

    stat: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        vc = str(r.get("vc") or "").strip() or NO_VC
        d = stat.setdefault(vc, {"cn": "", "nm_ids": set(), "shops": set(),
                                 "orders": 0, "returns": 0, "pending": 0, "unknown": 0})
        if r.get("cn") and not d["cn"]:
            d["cn"] = r["cn"]
        nm = str(r.get("nm_id") or "").strip()
        if nm:
            d["nm_ids"].add(nm)
        if r.get("shop_label"):
            d["shops"].add(r["shop_label"])
        d["orders"] += 1
        k = r.get("kind") or classify_status(r.get("status"))
        if k == "return":
            d["returns"] += 1
        elif k == "pending":
            d["pending"] += 1
        elif k == "unknown":
            d["unknown"] += 1

    sku_map = cn_stock_sku or {}
    out: List[Dict[str, Any]] = []
    for vc, d in stat.items():
        done = d["orders"] - d["pending"]
        cn = d["cn"]
        out.append({
            "vc": vc, "cn": cn,
            "orders": d["orders"], "returns": d["returns"], "pending": d["pending"],
            "unknown": d["unknown"],
            "nm_ids": sorted(d["nm_ids"], key=_nm_sort_key), "shops": sorted(d["shops"]),
            "stock_sku": sku_map.get(cn, "") if cn else "",
            "rate": (d["returns"] / d["orders"]) if d["orders"] else None,
            "rate_done": (d["returns"] / done) if done > 0 else None,
        })
    out.sort(key=lambda r: (str(r["vc"]).startswith("("), -r["orders"], r["vc"]))
    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out


def print_table(by_vc: Sequence[Dict[str, Any]], top: int = 0) -> None:
    """控制台渲染 VC 表；`top`>0 只显示前 N 名（xlsx 始终全量）。"""
    print(f"\n[按供应商代码] 共 {len(by_vc)} 个（跨店合并；同一 vc 各店 nmId 不同）")
    if not by_vc:
        return
    print("  序号 | 供应商代码 | 商品中文名 | 订单数 | 退货数 | 退货率 | 完单口径 | 未完结 | "
          "涉及店铺 | WB商品码 | 库存SKU")
    shown = by_vc[:top] if top else by_vc
    for x in shown:
        rt = f"{x['rate']:.2%}" if x["rate"] is not None else "-"
        rd = f"{x['rate_done']:.2%}" if x["rate_done"] is not None else "-"
        print(f"  {x['rank']:>3} | {x['vc']} | {x['cn'] or '-'} | {x['orders']} | {x['returns']} | "
              f"{rt} | {rd} | {x['pending']} | {'、'.join(x['shops']) or '-'} | "
              f"{','.join(x['nm_ids']) or '-'} | {x.get('stock_sku') or '-'}")
    if top and len(by_vc) > top:
        print(f"  ... 其余 {len(by_vc) - top} 个供应商代码见 xlsx")

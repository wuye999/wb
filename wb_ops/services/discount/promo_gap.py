# -*- coding: utf-8 -*-
"""
wb_ops 推广 × 销量 双向错配审计（promo-gap，只读）

用**同一份数据抓取**审计两个方向：

- **正向 `gap`（该推没推）**：某供应商代码（`BCS编号`）在**目标店铺的 7 天单数合计** ≥ 阈值，
  但某个目标店**没在推广它** → 该店该补推。
- **反向 `waste`（在推但该关）**：某供应商代码**在目标店铺的 7 天单数合计** < 阈值（含 0），
  则它在这些店**所有「在投」活动**里的推广都该关掉。

口径（2026-09-23 与用户确认，**已按用户修正为「合计」口径**）：
- 🔴 **销量判据 = 该 vc 在「目标店铺集合」的 7 天单数合计**（默认目标店铺 = 袁州1/2/3），
  **不是单店单数**。理由：同一供应商代码在不同店铺的 `wb编号`(nmId) 不同，但它就是同一个商品 ——
  要**合计**够量才算「值得推广」。合计范围 = 本次审计的目标店铺（`--shops`，默认 9352/9353/9356）。
  - ⚠ 合计时**一条登记记录只计一次**（若其 `店铺` 多选含多个目标店，也不重复计数）；
    「各店拆分」另按店统计（同一条记录对每个含它的店各计一次）。两者口径不同，输出里分别标注。
- **推广侧仍按店**（推广活动本身就是按店的）：正向判断「哪个目标店没推」，反向列出「哪家店哪个活动的哪个商品要关」。
- 阈值 `--min N`（默认 4）：正向 = 合计 ≥ N；反向 = 合计 < N。
- 「已推广」= 该店任一活动里出现过（默认 `status=[4,9,11]`，**含 11=暂停**）。
- 反向清单**只把「在投(status=9)」活动的商品列为「建议关闭」**；暂停中的活动已停，仅计数提示。
- 被推广但 nm **本地真源反查不到供应商代码** → 归「无法判定」组，**不推断、不混进建议关闭**。

数据源：飞书「订单登记」（**跨域只经 `order_svc` 门面**）、各目标店 cmp 广告推广
（`adverts.shop_adverted`）、各目标店 BCS 快照。全程**只读**：不写飞书、不自动关闭推广、不调 BCS 写接口。

可编程调用（供其它脚本 import；全部显式关键字参数）：
    from wb_ops.services.discount.promo_gap import promo_gap
    r = promo_gap(days=7, min_orders=4, mode="both")
    print(r["stats"]); print(r["waste_rows"][:3])
"""
import csv
import os
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import wb_ads_client as ads_api
from wb_ops.storage.mapping_repo import MappingRepository
from wb_ops.storage.product_repo import ProductSnapshotRepository
from . import adverts as adverts_mod

SHOP_SLEEP = 0.5
DEFAULT_MIN_ORDERS = 4
DEFAULT_TABLE = "订单登记"
ACTIVE_STATUS_ID = 9        # 2026-09-23 实测：9=在投
PAUSED_STATUS_ID = 11       # 11=暂停
KNOWN_STATUS_IDS = (ACTIVE_STATUS_ID, PAUSED_STATUS_ID)

MODE_GAP, MODE_WASTE, MODE_BOTH = "gap", "waste", "both"
ALL_MODES = (MODE_GAP, MODE_WASTE, MODE_BOTH)

GROUP_LISTED = "可直接补推"
GROUP_UNLISTED = "需先上架"
VERDICT_CLOSE = "建议关闭"
VERDICT_PAUSED = "已暂停无需操作"
VERDICT_UNKNOWN = "无法判定"

GAP_CSV_FIELDS = ["方向", "店铺", "店铺ID", "分组", "BCS编号", "商品中文名", "三店合计单数",
                  "该店单数", "各店单数拆分", "本店nmId", "备注"]
WASTE_CSV_FIELDS = ["店铺", "店铺ID", "活动ID", "活动名", "活动状态ID", "结算方式", "预算", "商品nmId",
                    "供应商代码", "商品中文名", "三店合计单数", "该店单数", "各店单数拆分",
                    "该nm本店被推广活动数", "判定", "备注"]
WASTE_CAMP_FIELDS = ["店铺", "店铺ID", "活动ID", "活动名", "活动状态ID", "结算方式", "预算",
                     "被推广商品数", "建议关闭数", "是否整活动建议关闭"]


# ---------------- 纯函数（离线可测，不触网） ----------------
def resolve_min(min_orders: Any, default: int = DEFAULT_MIN_ORDERS) -> int:
    """阈值解析：None/空串 → 默认值；非法或 < 1 → ValueError"""
    if min_orders is None or (isinstance(min_orders, str) and not str(min_orders).strip()):
        return default
    try:
        n = int(str(min_orders).strip())
    except (TypeError, ValueError):
        raise ValueError(f"--min 必须是正整数，实际收到: {min_orders!r}")
    if n < 1:
        raise ValueError(f"--min 必须 >= 1，实际收到 {n}")
    return n


def select_shops(all_shops: Sequence[Dict[str, Any]], tokens: Any = None
                 ) -> List[Tuple[Dict[str, Any], int, str]]:
    """凭证店铺 → [(shop_dict, shop_id, shop_name)]。

    `tokens` 支持 '9352' / '袁州1' / '9352,袁州3' / ['9352','袁州3']；空 = 全部。
    数字按 shopId 匹配，非数字按 shopName（店铺短名）匹配。
    """
    shops: List[Tuple[Dict[str, Any], int, str]] = []
    for s in (all_shops or []):
        shops.append((s, common.to_int(s.get("shopId") or s.get("shop_id")),
                      (s.get("shopName") or "").strip()))
    if not tokens:
        return shops
    if isinstance(tokens, str):
        items = [t.strip() for t in tokens.replace("，", ",").split(",") if t.strip()]
    elif isinstance(tokens, (list, tuple, set)):
        items = [str(t).strip() for t in tokens if str(t).strip()]
    else:
        items = [str(tokens).strip()]
    if not items:
        return shops
    want_ids: Set[int] = {int(t) for t in items if t.isdigit()}
    want_names: Set[str] = {t for t in items if not t.isdigit()}
    return [x for x in shops if x[1] in want_ids or (x[2] and x[2] in want_names)]


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


# ---------------- 读数据（薄封装，便于测试替身） ----------------
def load_shop_snapshot(shop_id: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """本店在架快照 {vc: row}；文件不存在返回 None"""
    return ProductSnapshotRepository.load_shop_rows(shop_id)


def collect_adverted(shop: Dict[str, Any], root_version: str, statuses: Any = None,
                     page_size: int = 100, max_pages: int = 50, limit: int = 0,
                     with_cn: bool = True) -> Dict[str, Any]:
    """本店「已被推广」明细 + 去重 vc 集合（同域直连 adverts 库入口）"""
    return adverts_mod.shop_adverted(shop, root_version, statuses=statuses, page_size=page_size,
                                     max_pages=max_pages, limit=limit, with_cn=with_cn)


def _empty_shop(sid: int, name: str, reason: str) -> Dict[str, Any]:
    """被跳过的店铺占位（保证返回结构一致、可核对）"""
    return {"shop_id": sid, "shop_name": name, "skip_reason": reason, "orders_rows": 0,
            "campaigns": 0, "total": -1, "truncated": False, "nm_unresolved": [],
            "missing_detail_campaigns": [], "adverted_total": 0}


# ---------------- 主库入口 ----------------
def promo_gap(days: int = 7, begin: str = "", end: str = "", date: str = "",
              min_orders: Any = DEFAULT_MIN_ORDERS, shops: Any = None, statuses: Any = None,
              mode: str = MODE_GAP, include_not_listed: bool = True, with_cn: bool = True,
              url: str = "", table: str = DEFAULT_TABLE, page_size: int = 100,
              max_pages: int = 50, limit: int = 0, write_csv_file: bool = True) -> Dict[str, Any]:
    """★ 库入口：推广 × 销量双向错配审计（只读）。

    **销量判据 = 该 vc 在目标店铺（默认袁州1/2/3）的 7 天单数合计**，非单店单数。

    参数（全部显式关键字）：
      days/begin/end/date  时间窗口（默认近 7 天含今天；基准列 = 飞书「日期」= 付款时间）
      min_orders           阈值（默认 4）：gap = 合计 ≥ N；waste = 合计 < N
      shops                目标店铺（= 合计范围，也是推广侧审计范围）：'9352' / '袁州1' / ['9352','袁州3']
      statuses             推广活动状态（默认 (4,9,11)，含暂停）—— 仅影响「已推广」判定
      mode                 'gap' / 'waste' / 'both' —— **只影响打印与写文件**，返回里两个方向都在
      include_not_listed   gap 方向是否把「需先上架」组也写进 `gap_rows`
      with_cn              是否用本地映射真源补全缺失的中文名
      url/table            飞书表格地址与表名（默认读 credentials）
      page_size/max_pages/limit  推广活动分页参数

    返回 dict：{'begin','end','min_orders','statuses','active_status','table','mode','shop_labels',
                 'feishu_stats','shops':[...],'gap_rows','gap_listed','gap_unlisted_all',
                 'waste_rows','waste_paused','waste_unknown','waste_by_nm','waste_by_campaign',
                 'stats':{'gap','waste'},'warnings','csv_path'}
    """
    min_orders = resolve_min(min_orders)
    mode = (mode or MODE_GAP).strip().lower()
    if mode not in ALL_MODES:
        raise ValueError(f"mode 只能是 {'/'.join(ALL_MODES)}，实际收到: {mode!r}")
    statuses = tuple(statuses) if statuses else ads_api.STATUS_DEFAULT

    cred = credentials.get()
    all_shops = cred.wb_shops()
    if not all_shops:
        raise RuntimeError("credentials.json 中没有已填 cookie 的店铺")
    targets = select_shops(all_shops, shops)
    if not targets:
        raise RuntimeError(f"指定店铺 {shops!r} 未在凭证中找到已填 cookie 的店铺")
    labels = [name for _, _, name in targets]

    # 飞书只读一次；跨域只经 order_svc 门面（REUSE_GUIDE 铁律 3）
    from wb_ops.services.order_svc import order_svc
    fs = order_svc.feishu_register_stats(days=days, begin=begin, end=end, date=date,
                                        shops=None, url=url, table=table, with_cn=False)
    rows = fs["raw_rows"]
    begin, end = fs["begin"], fs["end"]
    resolve_cn = MappingRepository.build_vc_resolver()[0] if with_cn else None

    per_shop: List[Dict[str, Any]] = []
    warnings: List[str] = []
    adverted_by_shop: Dict[int, Set[str]] = {}
    snapshots: Dict[int, Optional[Dict[str, Dict[str, Any]]]] = {}
    adv_products: List[Dict[str, Any]] = []
    for shop, sid, name in targets:
        try:
            adv = collect_adverted(shop, cred.root_version, statuses, page_size=page_size,
                                   max_pages=max_pages, limit=limit, with_cn=with_cn)
        except common.CookieExpiredError as e:
            warnings.append(f"店铺 {name}: {e}（该店中止，继续下一店；cookie 失效请跑 wb.py cookies-update）")
            per_shop.append(_empty_shop(sid, name, "cookie_expired"))
            adverted_by_shop[sid] = set()
            snapshots[sid] = None
            time.sleep(SHOP_SLEEP)
            continue
        except Exception as e:
            warnings.append(f"店铺 {name} 处理失败: {e}（该店中止，继续下一店）")
            per_shop.append(_empty_shop(sid, name, "error"))
            adverted_by_shop[sid] = set()
            snapshots[sid] = None
            time.sleep(SHOP_SLEEP)
            continue

        adverted_by_shop[sid] = set(adv["vcs"].keys())
        snapshots[sid] = load_shop_snapshot(sid)
        for p in adv["products"]:
            adv_products.append({**p, "shop_id": sid, "shop_name": name})
        orders_rows = sum(1 for r in rows if name in (r.get("shops") or []))
        per_shop.append({
            "shop_id": sid, "shop_name": name, "skip_reason": "",
            "orders_rows": orders_rows, "campaigns": adv["campaign_count"], "total": adv["total"],
            "truncated": adv["truncated"], "nm_unresolved": adv["nm_unresolved"],
            "missing_detail_campaigns": adv["missing_detail_campaigns"],
            "adverted_total": len(adv["vcs"]),
        })

        if adv["no_snapshot"]:
            warnings.append(f"店铺 {name} 无 BCS 快照（{config.shop_json_path(sid)} 不存在）→ "
                            f"「是否在架」无法判断，候选商品全部归入「需先上架」；需最新请先跑 python wb.py fetch")
        if adv["truncated"]:
            warnings.append(f"店铺 {name} 推广活动分页未取全：平台口径 {adv['total']} 个，实取 "
                            f"{adv['campaign_count']} 个（会漏判「已推广」也会漏列该关的；建议加大 --page-size/--max-pages）")
        for cid in adv["missing_detail_campaigns"]:
            warnings.append(f"店铺 {name} 活动 {cid} 声明有商品但未下发明细（stocks.products 为空），其商品未计入")
        if adv["nm_unresolved"]:
            shown = ",".join(str(x) for x in adv["nm_unresolved"][:10])
            warnings.append(f"店铺 {name} 有 {len(adv['nm_unresolved'])} 个被推广 nmId 在本地真源未收录"
                            f"（正向可能「假缺口」/反向归入「无法判定」）：{shown}")
        time.sleep(SHOP_SLEEP)

    gap = build_gap(targets, rows, adverted_by_shop, snapshots, min_orders=min_orders,
                    resolve_cn=resolve_cn, with_cn=with_cn)
    waste = build_waste(targets, rows, adv_products, min_orders=min_orders, with_cn=with_cn)

    for st, sid, cid in waste.get("waste_unknown_status") or []:
        nm = next((s["shop_name"] for s in per_shop if s["shop_id"] == sid), str(sid))
        warnings.append(f"店铺 {nm} 活动 {cid} 出现未识别的活动状态 status={st}，已按「非在投」处理，请人工核对")
    for s in per_shop:
        if s["orders_rows"] == 0 and not s["skip_reason"]:
            warnings.append(f"店铺 {s['shop_name']} 在「{table}」窗口 {begin}~{end} 内 0 条记录 ⚠ "
                            f"反向结论会把该店全部在投商品判为「0 单该关」，请先核对店铺短名是否与表内一致"
                            f"（当前用「{s['shop_name']}」）")

    gap_rows = list(gap["gap_listed"]) + (list(gap["gap_unlisted"]) if include_not_listed else [])

    stats = {
        "gap": {
            "目标店铺": "、".join(labels),
            "跳过店铺数": sum(1 for s in per_shop if s["skip_reason"]),
            "飞书命中记录": len(rows),
            **gap["gap_stats"],
        },
        "waste": {
            "目标店铺": "、".join(labels),
            "被推广商品位": waste["waste_stats"]["被推广商品位"],
            "建议关闭": waste["waste_stats"]["建议关闭"],
            "已暂停无需操作": waste["waste_stats"]["已暂停无需操作"],
            "无法判定": waste["waste_stats"]["无法判定"],
            "涉及活动数": len({(x["shop_id"], x["campaign_id"]) for x in waste["waste_close"]}),
        },
    }

    csv_path = {"gap": "", "waste": "", "waste_campaigns": ""}
    if write_csv_file:
        if mode in (MODE_GAP, MODE_BOTH) and gap_rows:
            csv_path["gap"] = write_csv(gap_rows, "gap")
        if mode in (MODE_WASTE, MODE_BOTH) and waste["waste_close"]:
            csv_path["waste"] = write_csv(waste["waste_close"], "waste")
            csv_path["waste_campaigns"] = write_csv(waste["waste_by_campaign"], "campaign")

    return {
        "begin": begin, "end": end, "min_orders": min_orders, "statuses": list(statuses),
        "active_status": ACTIVE_STATUS_ID, "table": table, "mode": mode,
        "shop_labels": labels, "feishu_stats": fs["stats"],
        "shops": per_shop,
        "gap_rows": gap_rows, "gap_listed": gap["gap_listed"], "gap_unlisted_all": gap["gap_unlisted"],
        "waste_rows": waste["waste_close"], "waste_paused": waste["waste_paused"],
        "waste_unknown": waste["waste_unknown"],
        "waste_by_nm": waste["waste_by_nm"], "waste_by_campaign": waste["waste_by_campaign"],
        "stats": stats, "warnings": warnings, "csv_path": csv_path,
    }


# ---------------- CSV ----------------
def write_csv(rows: Sequence[Dict[str, Any]], kind: str = "gap") -> str:
    """写 CSV（utf-8-sig）；`kind` ∈ {'gap','waste','campaign'}；返回路径"""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    if kind == "gap":
        name, fields = f"推广缺口_{ts}.csv", GAP_CSV_FIELDS

        def row_of(x):
            return {"方向": "gap（该推没推）", "店铺": x["shop_name"], "店铺ID": x["shop_id"],
                    "分组": x["group"], "BCS编号": x["vc"], "商品中文名": x["cn"],
                    "三店合计单数": x["orders"], "该店单数": x["shop_orders"],
                    "各店单数拆分": x["shop_split"], "本店nmId": x["nm_id"], "备注": x["备注"]}
    elif kind == "waste":
        name, fields = f"推广待关_{ts}.csv", WASTE_CSV_FIELDS

        def row_of(x):
            return {"店铺": x["shop_name"], "店铺ID": x["shop_id"], "活动ID": x["campaign_id"],
                    "活动名": x["campaign_name"], "活动状态ID": x["status_id"],
                    "结算方式": x["payment_model"], "预算": x["budget"], "商品nmId": x["nm"],
                    "供应商代码": x["vc"], "商品中文名": x["cn"],
                    "三店合计单数": x["orders"], "该店单数": x["shop_orders"],
                    "各店单数拆分": x["shop_split"],
                    "该nm本店被推广活动数": x["n_campaigns"],
                    "判定": x["verdict"], "备注": x["备注"]}
    elif kind == "campaign":
        name, fields = f"推广待关_按活动汇总_{ts}.csv", WASTE_CAMP_FIELDS

        def row_of(x):
            return {k: x.get(k) for k in fields}
    else:
        raise ValueError(f"未知的 CSV 类型: {kind!r}")

    path = os.path.join(config.LOG_DIR, name)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in rows:
            w.writerow(row_of(x))
    return path


# ---------------- 控制台打印 ----------------
def _print_header(r: Dict[str, Any]) -> None:
    label = {"gap": "gap（该推没推）", "waste": "waste（在推广但合计单数<阈值 → 建议关闭）",
             "both": "gap + waste（双向）"}[r["mode"]]
    print(f"[区间] {r['begin']} ~ {r['end']}（闭区间；基准列=日期/付款时间）｜方向={label}"
          f"｜阈值 --min {r['min_orders']}")
    print(f"       销量判据 = 「{'、'.join(r['shop_labels'])}」的**合计单数**（非单店）"
          f"｜「已推广」口径=status{r['statuses']}（含暂停）"
          f"｜反向「建议关闭」判定=在投 status{r['active_status']}")
    print("[明细] 飞书未命中原因（显式标注）: " +
          (" / ".join(f"{k}={v}" for k, v in (r["feishu_stats"] or {}).items()
                      if k != "总记录" and v) or "无"))


def _print_gap(r: Dict[str, Any], top: int) -> None:
    st = r["stats"]["gap"]
    print(f"\n[汇总·gap] 飞书命中 {st['飞书命中记录']} 条记录｜合计单数≥{r['min_orders']} 的供应商代码 "
          f"{st['候选vc数']} 个 → 缺口店铺位 {st['候选店铺位']} 个"
          f"（可直接补推 {st['可直接补推']} / 需先上架 {st['需先上架']}）｜跳过店铺 {st['跳过店铺数']} 个")
    if not r["gap_listed"] and not r["gap_unlisted_all"]:
        print("  [提示] 无缺口（合计达标的供应商代码在所有目标店都已推广）")
        return
    for group, title, note in ((r["gap_listed"], "组① 可直接补推（该店在架）", None),
                               (r["gap_unlisted_all"], "组② 需先上架（该店快照查不到该 vc）",
                                "⚠ 飞书「店铺」= 下单店铺，本组仅表示该店快照未收录该 vc，需人工确认")):
        if not group:
            continue
        print(f"  # {title}")
        if note:
            print(f"  {note}")
        print("  排名 | 店铺 | BCS编号 | 商品中文名 | 合计单数 | 该店单数 | 各店拆分 | 本店nmId | 备注")
        for i, x in enumerate(group[:top] if top else group, 1):
            print(f"  {i:>4} | {x['shop_name']} | {x['vc']} | {x['cn'] or '-'} | {x['orders']} | "
                  f"{x['shop_orders']} | {x['shop_split'] or '-'} | "
                  f"{x.get('nm_id') or '-'} | {x['备注'] or '-'}")
        if top and len(group) > top:
            print(f"  ... 该组其余 {len(group) - top} 个见 CSV")
    listed = [x["vc"] for x in r["gap_listed"]]
    if listed:
        print("  [补推vc] " + ",".join(dict.fromkeys(listed)))


def _print_waste(r: Dict[str, Any], top: int) -> None:
    st = r["stats"]["waste"]
    print(f"\n[汇总·waste] 被推广商品位 {st['被推广商品位']} 个 → 建议关闭 {st['建议关闭']} 个"
          f"（涉及 {st['涉及活动数']} 个活动）｜已暂停无需操作 {st['已暂停无需操作']} 个"
          f"｜无法判定 {st['无法判定']} 个")
    print(f"  [口径] 判据 = 「{'、'.join(r['shop_labels'])}」**合计单数 < {r['min_orders']}**"
          f"；只列「在投(status={r['active_status']})」活动，暂停活动已停无需操作")
    print("  ⚠ [风险] 商品在本店刚上架/尚未起量时也会进清单，请结合上架时间人工判断")
    if not r["waste_rows"]:
        print("  [提示] 无建议关闭项")
    else:
        print("  店铺 | 活动ID | 活动名 | 结算 | 预算 | 商品nmId | 供应商代码 | 商品中文名 | 合计/该店 | 各店拆分 | 备注")
        for x in (r["waste_rows"][:top] if top else r["waste_rows"]):
            print(f"  {x['shop_name']} | {x['campaign_id']} | {x['campaign_name'] or '-'} | "
                  f"{x['payment_model']} | {x['budget']} | {x['nm']} | {x['vc'] or '-'} | "
                  f"{x['cn'] or '-'} | {x['orders']}/{x['shop_orders']} | {x['shop_split'] or '-'} | {x['备注']}")
        if top and len(r["waste_rows"]) > top:
            print(f"  ... 其余 {len(r['waste_rows']) - top} 个见 CSV")
    if r["waste_paused"]:
        print(f"  # 已暂停无需操作 —— {len(r['waste_paused'])} 个（活动已在停，不逐条列）")
    if r["waste_unknown"]:
        print(f"  # 无法判定（nm 本地真源未收录，无法关联销量）—— {len(r['waste_unknown'])} 个，需人工判断")
        print("  店铺 | 活动ID | 商品nmId | 俄文标题 | 备注")
        for x in r["waste_unknown"]:
            print(f"  {x['shop_name']} | {x['campaign_id']} | {x['nm']} | {(x['ru'] or '-')[:36]} | {x['备注']}")
    if r["waste_by_campaign"]:
        print("  # 活动级汇总（便于整活动关闭）")
        print("  店铺 | 活动ID | 活动名 | 状态 | 被推广商品数 | 建议关闭数 | 结论")
        for x in r["waste_by_campaign"]:
            print(f"  {x['shop_name']} | {x['campaign_id']} | {x['campaign_name'] or '-'} | "
                  f"{x['status_id']} | {x['被推广商品数']} | {x['建议关闭数']} | "
                  f"{x['是否整活动建议关闭'] or '-'}")
    if r["waste_by_nm"]:
        print("  # 商品级汇总（该 nm 在本店出现在几个活动）")
        print("  店铺 | 商品nmId | 供应商代码 | 商品中文名 | 合计单数 | 该店单数 | 本店活动数")
        for x in r["waste_by_nm"]:
            print(f"  {x['shop_name']} | {x['nm']} | {x['vc'] or '-'} | {x['cn'] or '-'} | "
                  f"{x['orders']} | {x['shop_orders']} | {x['n_campaigns']}")


def run(args: Any) -> int:
    """`wb.py promo-gap`：推广 × 销量双向错配审计（只读）。"""
    common.ensure_utf8_stdout()
    mode = (getattr(args, "mode", "") or MODE_GAP).strip().lower() or MODE_GAP
    top = common.to_int(getattr(args, "top", 0))
    listed_only = bool(getattr(args, "listed_only", False))
    if listed_only and mode == MODE_WASTE:
        print("[提示] --listed-only 仅对 gap 方向生效，waste 模式下已忽略")
    opt = adverts_mod.build_options(args)          # 复用 --status/--no-cn/分页参数解析（铁律 6）

    print(f"[开始] 方向={mode}｜阈值 --min {getattr(args, 'min', DEFAULT_MIN_ORDERS)}"
          f"｜正在读取飞书「订单登记」与各目标店推广活动（只读）…")
    try:
        r = promo_gap(days=getattr(args, "days", 7), begin=getattr(args, "begin", ""),
                      end=getattr(args, "end", ""), date=getattr(args, "date", ""),
                      min_orders=getattr(args, "min", DEFAULT_MIN_ORDERS),
                      shops=getattr(args, "shops", "") or "", statuses=opt["statuses"], mode=mode,
                      include_not_listed=not listed_only, with_cn=opt["with_cn"],
                      url=getattr(args, "url", ""),
                      table=(getattr(args, "table", "") or "").strip() or DEFAULT_TABLE,
                      page_size=opt["page_size"], max_pages=opt["max_pages"], limit=opt["limit"])
    except ValueError as e:
        print(f"[错误] {e}")
        return 1
    except RuntimeError as e:
        print(f"[错误] {str(e)[:300]}")
        return 1

    _print_header(r)
    if mode in (MODE_GAP, MODE_BOTH):
        _print_gap(r, top)
    if mode in (MODE_WASTE, MODE_BOTH):
        _print_waste(r, top)

    if r["warnings"]:
        print("\n[警告]")
        for w in r["warnings"]:
            print(f"  - {w}")
        print("[提示] 以上告警（未收录 nmId / 分页未取全 / 活动未下发明细）会让「已推广」集合偏小 → "
              "可能造成「假缺口」或漏列该关的，建议先跑 python wb.py fetch 后重试")

    if mode in (MODE_GAP, MODE_BOTH):
        if r["csv_path"]["gap"]:
            print(f"\n[日志] 缺口明细 {len(r['gap_rows'])} 行 → {r['csv_path']['gap']}")
        else:
            print("\n[提示] gap 方向无缺口（未写 CSV）")
    if mode in (MODE_WASTE, MODE_BOTH):
        if r["csv_path"]["waste"]:
            print(f"[日志] 待关明细 {len(r['waste_rows'])} 行 → {r['csv_path']['waste']}")
            print(f"[日志] 活动级汇总 {len(r['waste_by_campaign'])} 行 → {r['csv_path']['waste_campaigns']}")
        else:
            print("[提示] waste 方向无建议关闭项（未写 CSV）")
    return 0

# -*- coding: utf-8 -*-
"""
wb_ops WB FBS「配送中」清单与商品采集统计（只读）

用途：遍历各店 WB 卖家后台 FBS「配送中」tab，拉取**全部清单**及每张清单下的**全部商品**，
按 **店铺 / 时间段（清单 createdAt）/ 商品状态（默认 shipment=待发货）** 筛选后，
统计「商品中文名的数量」等，输出控制台汇总 + CSV 明细。

口径（与用户确认，2026-10-08）：
- 时间基准 = **清单的 createdAt**（闭区间；默认**不过滤**，用 --days/--date/--begin/--end 才启用）。
- 商品状态默认只保留 `shipment`（「待发货」）；`--item-state all` 不过滤；`--include-canceled` 并入 `cancel`。
  ⚠ `orders/delivery` 接口返回该清单下**全部**订单（含已取消/已售出），必须按 `state.code` 过滤。
- 商品中文名由 vendorCode(`article`) 反查本地映射真源（`MappingRepository.build_vc_resolver`，
  优先级 override > known > 前缀码兜底）；查不到归 `(中文名未知)` 桶且排序置尾。
- 聚合口径：**商品数 = 明细行数**；**供应商代码数 = 去重 vendorCode**；**清单数 = 去重 supplyID**。

可编程调用（供其它脚本 import；全部显式关键字参数）：
    from wb_ops.services.order.fbs_delivery import delivery_goods
    r = delivery_goods(item_states="shipment", shops="9352")
    for row in r["by_cn"]:
        print(row["rank"], row["cn"], row["item_count"])

纯只读：不触发任何写操作，不做写后验证，不自动同步/合并映射表。
"""
import csv
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import wb_client as wb_api
from wb_ops.adapters import wb_supply_client as wb_supply
from wb_ops.storage.mapping_repo import MappingRepository

DATE_FMT = "%Y-%m-%d"
NO_CN = "(中文名未知)"          # 中文名解析失败 → 单独成桶，不静默丢弃
ALL_STATES = "all"
SHOP_SLEEP = 0.5               # 店铺间串行限速
DEFAULT_ITEM_STATE = "shipment"  # 「待发货」

CSV_DETAIL_FIELDS = [
    "店铺", "店铺ID", "清单ID", "清单状态", "清单创建时间", "清单仓库",
    "商品状态", "订单ID", "WB商品码", "供应商代码", "商品中文名", "库存SKU", "俄文标题", "尺码",
    "原价", "折后价", "贴标ID", "是否取消", "是否已交接", "商品创建时间",
]
CSV_STAT_FIELDS = ["排名", "商品中文名", "库存SKU", "供应商代码数", "商品数", "清单数", "涉及店铺"]


# ---------------- 纯函数（离线可测，不触网） ----------------
def parse_supply(s: Dict[str, Any]) -> Dict[str, Any]:
    """清单对象 → 归一化行。"""
    st = s.get("state") or {}
    created = str(s.get("createdAt") or "")
    dw = s.get("destinationWarehouse") or {}
    return {
        "supply_id": str(s.get("supplyID") or ""),
        "name": str(s.get("name") or ""),
        "created_at": created,
        "created_day": created[:10],
        "state_code": str(st.get("code") or ""),
        "state_title": str(st.get("title") or ""),
        "orders_cnt": common.to_int(s.get("ordersCnt"), 0),
        "parcels_cnt": common.to_int(s.get("parcelsCnt"), 0),
        "boxes_cnt": common.to_int(s.get("boxesCnt"), 0),
        "warehouse": str(s.get("warehouse") or ""),
        "dest_name": str(dw.get("name") or ""),
        "cargo_type": str(s.get("cargoType") or ""),
    }


def parse_order(o: Dict[str, Any], supply_id: str) -> Dict[str, Any]:
    """商品/订单对象 → 归一化行（`article` 即 vendorCode）。"""
    st = o.get("state") or {}
    created = str(o.get("createdAt") or "")
    return {
        "order_id": str(o.get("id") or ""),
        "supply_id": supply_id,
        "nm_id": str(o.get("nmId") or ""),
        "vc": str(o.get("article") or ""),
        "title": str(o.get("title") or ""),
        "size": str(o.get("size") or ""),
        "price": str(o.get("price") or ""),
        "converted_price": str(o.get("convertedPrice") or ""),
        "sticker_id": str(o.get("stickerID") or ""),
        "state_code": str(st.get("code") or ""),
        "state_title": str(st.get("title") or ""),
        "created_at": created,
        "created_day": created[:10],
        "is_canceled": bool(o.get("isCanceled")),
        "is_transferred": bool(o.get("isTransferred")),
    }


def _norm_day(s: str, flag: str) -> str:
    """YYYY-M-D → YYYY-MM-DD；空/非法抛 ValueError"""
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{flag} 日期不能为空")
    try:
        return datetime.strptime(s, DATE_FMT).strftime(DATE_FMT)
    except ValueError:
        raise ValueError(f"{flag} 日期格式应为 YYYY-MM-DD，实际收到: {s!r}")


def resolve_window(days: int = 0, begin: str = "", end: str = "", date: str = "") -> Tuple[str, str]:
    """推导时间窗口（闭区间）；**全空 → ("", "") 表示不过滤**。

    优先级：begin/end 成对 > date 单天 > days 近 N 天。
    只给 --end → ValueError（与 feishu-vc-stats 同规矩）。
    """
    begin = (begin or "").strip()
    end = (end or "").strip()
    date = (date or "").strip()
    if end and not (begin or date):
        raise ValueError("--end 必须与 --begin 或 --date 同用（单独 --end 无法确定起点，已中止）")
    if begin or end or date:
        b = _norm_day(begin or date or end, "--begin/--date")
        e = _norm_day(end or date or begin, "--end/--date")
        if b > e:
            b, e = e, b
        return b, e
    n = common.to_int(days, 0)
    if n and n >= 1:
        today = datetime.now().date()
        return (today - timedelta(days=n - 1)).strftime(DATE_FMT), today.strftime(DATE_FMT)
    return "", ""


def in_window(day: str, begin: str, end: str) -> bool:
    """day 是否落在闭区间 [begin,end]；begin/end 任一为空 → 恒 True（不过滤）。"""
    if not begin or not end:
        return True
    return bool(day) and begin <= day <= end


def parse_states(token: str, include_canceled: bool = False) -> Set[str]:
    """状态过滤参数 → 集合；空集 = 不过滤（`all` 亦视为不过滤）。"""
    raw = str(token or "").strip()
    if not raw:
        states: Set[str] = {DEFAULT_ITEM_STATE}
    else:
        states = {s.strip().lower() for s in re.split(r"[,\s]+", raw) if s.strip()}
    if ALL_STATES in states:
        return set()
    if include_canceled:
        states.add("cancel")
    return states


def match_state(code: str, wanted: Set[str]) -> bool:
    """wanted 为空 → 不过滤；否则要求 code ∈ wanted。"""
    if not wanted:
        return True
    return (code or "") in wanted


def select_shops(shops: Sequence[Dict[str, Any]], token: str) -> List[Dict[str, Any]]:
    """店铺过滤，'9352' / '袁州1' / '9352,袁州3' 均可；空=全部。"""
    token = (token or "").strip()
    if not token:
        return list(shops)
    items = [t for t in re.split(r"[,\s]+", token) if t]
    out: List[Dict[str, Any]] = []
    for s in shops:
        sid = str(common.to_int(s.get("shopId")))
        name = str(s.get("shopName") or "")
        for t in items:
            if t == sid or (name and t == name):
                out.append(s)
                break
    return out


def load_cn_stock_sku() -> Dict[str, str]:
    """商品价格表 → {商品中文名: 库存SKU}（第 8 列；缺列/空值 → ""）。

    供「按中文名统计」补「库存SKU」列；表缺失或读取异常时返回空 dict（不中断主流程）。
    """
    out: Dict[str, str] = {}
    try:
        for it in MappingRepository.load_boss():
            cn = (it.get("cn") or "").strip()
            if cn and cn not in out:
                out[cn] = (it.get("stock_sku") or "").strip()
    except Exception as ex:
        print(f"  [警告] 读取商品价格表「库存SKU」失败（{type(ex).__name__}: {ex}）；库存SKU 列将留空")
    return out


def aggregate_by_cn(orders: Sequence[Dict[str, Any]],
                    cn_stock_sku: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """按「商品中文名」聚合（核心产出）。

    排序：商品数降序 → 同数按中文名升序；`(中文名未知)` 等未归类桶恒排最后。
    `cn_stock_sku` = {商品中文名: 库存SKU}（读商品价格表），为每行补「库存SKU」列。
    """
    stat: Dict[str, Dict[str, Any]] = {}
    for o in orders:
        cn = o.get("cn") or NO_CN
        d = stat.setdefault(cn, {"cn": cn, "vcs": set(), "items": 0, "supplies": set(), "shops": set()})
        if o.get("vc"):
            d["vcs"].add(o["vc"])
        d["items"] += 1
        if o.get("supply_id"):
            d["supplies"].add(o["supply_id"])
        if o.get("shop_name"):
            d["shops"].add(o["shop_name"])
    sku_map = cn_stock_sku or {}
    out: List[Dict[str, Any]] = [
        {"cn": cn, "stock_sku": sku_map.get(cn, ""), "vc_count": len(d["vcs"]),
         "item_count": d["items"], "supply_count": len(d["supplies"]), "shops": sorted(d["shops"])}
        for cn, d in stat.items()
    ]
    out.sort(key=lambda r: (str(r["cn"]).startswith("("), -r["item_count"], r["cn"]))
    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out


def aggregate_by_vc(orders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按供应商代码(vendorCode)聚合。"""
    stat: Dict[str, Dict[str, Any]] = {}
    for o in orders:
        vc = o.get("vc") or "(无供应商代码)"
        d = stat.setdefault(vc, {"vc": vc, "items": 0, "supplies": set(), "shops": set(), "cn": ""})
        d["items"] += 1
        if o.get("supply_id"):
            d["supplies"].add(o["supply_id"])
        if o.get("shop_name"):
            d["shops"].add(o["shop_name"])
        if o.get("cn") and not d["cn"]:
            d["cn"] = o["cn"]
    out = [{"vc": vc, "cn": d["cn"], "item_count": d["items"],
            "supply_count": len(d["supplies"]), "shops": sorted(d["shops"])}
           for vc, d in stat.items()]
    out.sort(key=lambda r: (str(r["vc"]).startswith("("), -r["item_count"], r["vc"]))
    return out


def aggregate_by_shop(orders: Sequence[Dict[str, Any]], supplies: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按店铺聚合：清单数 / 商品数 / 中文名种类数。"""
    supp_cnt: Dict[str, int] = Counter(s.get("shop_name") or "" for s in supplies)
    items: Dict[str, int] = Counter(o.get("shop_name") or "" for o in orders)
    cns: Dict[str, Set[str]] = defaultdict(set)
    for o in orders:
        cns[o.get("shop_name") or ""].add(o.get("cn") or NO_CN)
    out = [{"shop": name, "supply_count": supp_cnt.get(name, 0),
            "item_count": items.get(name, 0), "cn_count": len(cns.get(name, set()))}
           for name in sorted(set(supp_cnt) | set(items))]
    out.sort(key=lambda r: -r["item_count"])
    return out


# ---------------- 采集入口 ----------------
def collect(days: int = 0, begin: str = "", end: str = "", date: str = "", shops: Any = None,
            item_states: str = DEFAULT_ITEM_STATE, supply_states: str = "",
            include_canceled: bool = False, limit: int = 0, max_pages: int = 50,
            with_cn: bool = True, interval: float = 0.3, verbose: bool = False) -> Dict[str, Any]:
    """★ 全流程（只读）：遍历店铺 → 拉清单(分页) → 逐清单拉商品(分页) → 过滤 → vc→中文名 → 聚合。

    返回 dict：{'begin','end','item_states','supply_states','rows','supplies','errors',
                 'per_shop','by_cn','by_vc','by_shop','item_state','supply_state'}
    失败抛 RuntimeError/ValueError，交由调用方处理。
    """
    cred = credentials.get()
    all_shops = cred.wb_shops()
    if not all_shops:
        raise RuntimeError("credentials.json 中没有已填 cookie 的店铺（请编辑 data/credentials.json 的 wb.shops）")
    pairs = select_shops(all_shops, shops or "")
    if not pairs:
        raise RuntimeError(f"指定店铺 {shops!r} 未在凭证中找到已填 cookie 的店铺")

    b, e = resolve_window(days=days, begin=begin, end=end, date=date)
    want_item = parse_states(item_states, include_canceled=include_canceled)
    want_supply = parse_states(supply_states) if str(supply_states or "").strip() else set()

    resolve_cn = None
    if with_cn:
        try:
            resolve_cn, _ = MappingRepository.build_vc_resolver()
        except Exception as ex:
            print(f"  [警告] 中文名真源（映射表）加载失败（{type(ex).__name__}: {ex}）；中文名将留空")
    cn_stock_sku = load_cn_stock_sku()   # {中文名: 库存SKU}（商品价格表第 8 列）

    rows: List[Dict[str, Any]] = []
    supp_rows: List[Dict[str, Any]] = []
    errors: List[Tuple[int, str, str]] = []
    per_shop: List[Dict[str, Any]] = []

    for shop in pairs:
        sid = common.to_int(shop.get("shopId"))
        name = str(shop.get("shopName") or sid)
        try:
            session = wb_api.make_session(shop, cred.root_version)
            supplies = wb_supply.iter_delivery_supplies(session, max_pages=max_pages,
                                                        limit=limit, verbose=verbose)
        except common.CookieExpiredError as ex:
            print(f"  [警告] 店铺 {name}: {ex}（该店中止，继续下一店；cookie 失效请跑 wb.py cookies-update）")
            per_shop.append({"shop": name, "shop_id": sid, "supply_count": 0, "item_count": 0, "error": str(ex)[:120]})
            continue
        except Exception as ex:
            print(f"  [警告] 店铺 {name} 处理失败: {ex}（该店中止，继续下一店）")
            per_shop.append({"shop": name, "shop_id": sid, "supply_count": 0, "item_count": 0, "error": str(ex)[:120]})
            continue

        kept, items_n = 0, 0
        for s in supplies:
            ps = parse_supply(s)
            if not in_window(ps["created_day"], b, e):
                continue
            if not match_state(ps["state_code"], want_supply):
                continue
            supp_rows.append({**ps, "shop_name": name, "shop_id": sid})
            kept += 1
            try:
                orders = wb_supply.iter_supply_orders(session, ps["supply_id"], max_pages=max_pages)
            except Exception as ex:
                errors.append((sid, ps["supply_id"], str(ex)[:200]))
                continue
            for o in orders:
                po = parse_order(o, ps["supply_id"])
                if not match_state(po["state_code"], want_item):
                    continue
                po["cn"] = resolve_cn(po["vc"], "") if resolve_cn else ""
                po["stock_sku"] = cn_stock_sku.get(po["cn"], "") if po["cn"] else ""
                po["shop_name"] = name
                po["shop_id"] = sid
                po["supply_state_code"] = ps["state_code"]
                po["supply_created_day"] = ps["created_day"]
                po["supply_warehouse"] = ps["warehouse"]
                rows.append(po)
                items_n += 1
            if interval:
                time.sleep(interval)
        per_shop.append({"shop": name, "shop_id": sid, "supply_count": kept,
                         "item_count": items_n, "error": ""})
        time.sleep(SHOP_SLEEP)

    return {
        "begin": b, "end": e,
        "item_states": sorted(want_item), "supply_states": sorted(want_supply),
        "rows": rows, "supplies": supp_rows, "errors": errors, "per_shop": per_shop,
        "by_cn": aggregate_by_cn(rows, cn_stock_sku),
        "by_vc": aggregate_by_vc(rows),
        "by_shop": aggregate_by_shop(rows, supp_rows),
        "item_state": Counter(o["state_code"] for o in rows),
        "supply_state": Counter(s["state_code"] for s in supp_rows),
    }


def delivery_goods(**kwargs) -> Dict[str, Any]:
    """★ 库入口（供其它脚本 import）：WB FBS「配送中」清单商品采集与统计（只读）。

    参数全部显式关键字，见 `collect(...)`。
    """
    return collect(**kwargs)


# ---------------- 输出 ----------------
def write_csv(orders: Sequence[Dict[str, Any]], by_cn: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    """明细 + 按中文名统计 → 两个 CSV（utf-8-sig，写 config.LOG_DIR）；返回 (明细路径, 统计路径)。

    走 `common.write_csv_preferred`：环境按文件名拦截（实测 2026-10-17：
    「配送中商品明细_*.csv」被拒）时自动回退 ASCII 别名，两者都失败返回 ""。
    """
    ts = time.strftime("%Y%m%d_%H%M%S")

    def _detail(f):
        w = csv.DictWriter(f, fieldnames=CSV_DETAIL_FIELDS)
        w.writeheader()
        for o in orders:
            w.writerow({
                "店铺": o.get("shop_name", ""), "店铺ID": o.get("shop_id", ""),
                "清单ID": o.get("supply_id", ""), "清单状态": o.get("supply_state_code", ""),
                "清单创建时间": o.get("supply_created_day", ""), "清单仓库": o.get("supply_warehouse", ""),
                "商品状态": o.get("state_code", ""), "订单ID": o.get("order_id", ""),
                "WB商品码": o.get("nm_id", ""), "供应商代码": o.get("vc", ""),
                "商品中文名": o.get("cn", ""), "库存SKU": o.get("stock_sku", ""),
                "俄文标题": o.get("title", ""),
                "尺码": o.get("size", ""), "原价": o.get("price", ""),
                "折后价": o.get("converted_price", ""), "贴标ID": o.get("sticker_id", ""),
                "是否取消": "是" if o.get("is_canceled") else "否",
                "是否已交接": "是" if o.get("is_transferred") else "否",
                "商品创建时间": o.get("created_at", ""),
            })

    def _stat(f):
        w = csv.DictWriter(f, fieldnames=CSV_STAT_FIELDS)
        w.writeheader()
        for r in by_cn:
            w.writerow({
                "排名": r["rank"], "商品中文名": r["cn"], "库存SKU": r.get("stock_sku", ""),
                "供应商代码数": r["vc_count"],
                "商品数": r["item_count"], "清单数": r["supply_count"],
                "涉及店铺": "、".join(r["shops"]),
            })

    detail_path = common.write_csv_preferred(
        config.LOG_DIR, "配送中商品明细_{ts}.csv", "fbs_delivery_detail_{ts}.csv", ts, _detail)
    stat_path = common.write_csv_preferred(
        config.LOG_DIR, "配送中按中文名统计_{ts}.csv", "fbs_delivery_by_cn_{ts}.csv", ts, _stat)
    return detail_path, stat_path


# ---------------- CLI 入口 ----------------
def run(args: Any) -> int:
    """`wb.py fbs-delivery`：采集 WB FBS「配送中」清单商品并按中文名统计（只读）。"""
    common.ensure_utf8_stdout()
    days = getattr(args, "days", 0)
    try:
        b, e = resolve_window(days=days, begin=getattr(args, "begin", ""),
                              end=getattr(args, "end", ""), date=getattr(args, "date", ""))
    except ValueError as err:
        print(f"[错误] {err}")
        return 1

    item_states = getattr(args, "item_state", DEFAULT_ITEM_STATE)
    supply_states = getattr(args, "supply_state", "")
    shops = getattr(args, "shops", "") or ""
    with_cn = not bool(getattr(args, "no_cn", False))
    top = common.to_int(getattr(args, "top", 0))
    limit = common.to_int(getattr(args, "limit", 0))
    max_pages = common.to_int(getattr(args, "max_pages", 50)) or 50
    interval = float(getattr(args, "interval", 0.3) or 0)
    include_canceled = bool(getattr(args, "include_canceled", False))

    win_txt = f"{b} ~ {e}" if (b and e) else "不按时间过滤（全部配送中）"
    print(f"[区间] {win_txt}（基准=清单 createdAt/UTC）"
          f"｜店铺={shops or '全部'}"
          f"｜商品状态={item_states or '全部'}{'（含已取消）' if include_canceled else ''}"
          f"｜清单状态={supply_states or '全部'}"
          f"｜中文名={'不解析' if not with_cn else '解析'}")

    try:
        r = collect(days=days, begin=getattr(args, "begin", ""), end=getattr(args, "end", ""),
                    date=getattr(args, "date", ""), shops=shops, item_states=item_states,
                    supply_states=supply_states, include_canceled=include_canceled,
                    limit=limit, max_pages=max_pages, with_cn=with_cn, interval=interval)
    except (RuntimeError, ValueError) as err:
        print(f"[错误] {err}")
        return 1

    rows, supps = r["rows"], r["supplies"]
    for ps in r["per_shop"]:
        extra = f"  [失败] {ps['error']}" if ps.get("error") else ""
        print(f"  === 店铺 {ps['shop']}({ps['shop_id']}) ===  清单 {ps['supply_count']} 个 → "
              f"命中商品 {ps['item_count']} 个{extra}")

    if not rows:
        print("\n该条件下没有命中的商品（检查 --shops/--days/--item-state）")
        return 0

    print(f"\n[汇总] 清单 {len(supps)} 个 | 商品行 {len(rows)} 行 | "
          f"去重 WB商品码 {len({o['nm_id'] for o in rows if o['nm_id']})} 个 | "
          f"去重供应商代码 {len({o['vc'] for o in rows if o['vc']})} 个 | "
          f"去重订单号 {len({o['order_id'] for o in rows if o['order_id']})} 个")
    print(f"[商品状态] " + "｜".join(f"{k or '-'}={v}" for k, v in r["item_state"].most_common()))
    if r["supply_state"]:
        print(f"[清单状态] " + "｜".join(f"{k or '-'}={v}" for k, v in r["supply_state"].most_common()))

    by_cn = r["by_cn"]
    print(f"\n  排名 | 商品中文名 | 库存SKU | 供应商代码数 | 商品数 | 清单数 | 涉及店铺")
    shown = by_cn[:top] if top else by_cn
    for row in shown:
        print(f"  {row['rank']:>3} | {row['cn']} | {row.get('stock_sku') or '-'} | {row['vc_count']} | "
              f"{row['item_count']} | {row['supply_count']} | {'、'.join(row['shops']) or '-'}")
    if top and len(by_cn) > top:
        print(f"  ... 其余 {len(by_cn) - top} 个中文名见 CSV")

    vcs = sorted({o["vc"] for o in rows if o["vc"]})
    nmids = sorted({o["nm_id"] for o in rows if o["nm_id"]})
    if vcs:
        print(f"\n[供应商代码] 去重 {len(vcs)} 个（英文逗号分隔，可直接复制）：")
        print(",".join(vcs))
    if nmids:
        print(f"\n[WB商品码] 去重 {len(nmids)} 个（英文逗号分隔）：")
        print(",".join(nmids))

    if r["errors"]:
        print(f"\n[警告] {len(r['errors'])} 个清单拉取失败（已跳过，不影响其它数据）")

    detail_path, stat_path = write_csv(rows, by_cn)
    print(f"\n[日志] 明细 {len(rows)} 行 → {detail_path or '（未生成，见上方警告）'}")
    print(f"[日志] 按中文名统计 {len(by_cn)} 行 → {stat_path or '（未生成，见上方警告）'}")
    return 0

# -*- coding: utf-8 -*-
"""
wb_ops WB「订单动态」近一月订单按商品中文名统计（只读）

用途：逐店拉取 WB 卖家后台「订单动态」(`order-feed/orders`) 时间窗内全部订单行，
按 **商品中文名** 聚合出 订单数 / 退货数 / 退货率 / 完单口径退货率 / 未完结 / 涉及店铺 /
供应商代码数 / 库存SKU，输出单个 xlsx 多 Sheet（汇总 + 按供应商代码 + 每店 + 订单明细）。
另按 **供应商代码(VC)** 跨店合并出独立子表：同一商品各店 vendorCode 相同、nmId 不同，
`WB商品码` 列全量列出该 vc 涉及的所有 nmId（见 `order_stats_vc.py`）。

口径（与用户确认，2026-10-27）：
- **订单数 = 明细行数**（接口一行=一件商品，不按订单号去重）。
- **退货数** = `order.status` ∈ {cancel, return, returnDefective}（大小写不敏感）。
- **未完结** = `order.status == created`；退货率 = 退货数/订单数；
  完单口径退货率 = 退货数/(订单数 − 未完结)，分母 ≤0 记 None（xlsx 写 "-"）。
- 时间基准 = **下单时间 created**；但接口的 `currentPeriod` 实测按 **updated 过滤**
  （2026-10-27 实测：3 天窗口返回 65 行，updated 全部在窗口内、created 仅 7 行在窗口内）
  ⇒ 服务端只用来「粗筛超集」，**必须本地再按 created 精确过滤**；历史区间还要把请求
  上界放宽到「今天」，否则下单在窗口内、之后才变状态的订单会被服务端漏掉。
- 默认**近 30 天含今天**，窗口日期按 `--timezone`（默认 America/Los_Angeles，对齐抓包）计算。
- 中文名由 vendorCode 反查本地映射真源（`MappingRepository.build_vc_resolver`，
  优先级 override > known > 前缀码兜底）；查不到归 `(中文名未知)` 桶且排序置尾。
- 未知 status **不猜**：计入订单数，不进退货/未完结，单独计数并显式告警。

可编程调用（供其它脚本 import；全部显式关键字参数）：
    from wb_ops.services.order.order_stats import order_stats
    r = order_stats(days=30, shops="9352")
    for row in r["by_cn"]:
        print(row["rank"], row["cn"], row["orders"], row["rate"])
    for row in r["by_vc"]:
        print(row["rank"], row["vc"], row["orders"], ",".join(row["nm_ids"]))

纯只读：不触发任何写操作，不做写后验证，不自动同步/合并映射表。
"""
import time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from wb_ops import common
from wb_ops import credentials
from wb_ops.adapters import wb_client as wb_api
from wb_ops.adapters import wb_order_feed_client as feed
from wb_ops.storage.mapping_repo import MappingRepository

from .fbs_delivery import NO_CN, load_cn_stock_sku, select_shops
from .order_stats_vc import aggregate_by_vc, print_table as print_vc_table

DATE_FMT = "%Y-%m-%d"
DEFAULT_DAYS = 30
DEFAULT_TZ = feed.DEFAULT_TZ
SHOP_SLEEP = 0.5                       # 店铺间串行限速

RETURN_STATUSES = frozenset({"cancel", "return", "returndefective"})
PENDING_STATUSES = frozenset({"created"})
DONE_STATUSES = frozenset({"buyout"})

# 本机无 tzdata（zoneinfo 不可用）⇒ 用固定偏移；LA 另按夏令时规则修正
TZ_OFFSET_MIN = {"America/Los_Angeles": -420, "Europe/Moscow": 180,
                 "Asia/Shanghai": 480, "UTC": 0}

KIND_RETURN, KIND_PENDING, KIND_DONE, KIND_UNKNOWN = "return", "pending", "done", "unknown"


# ---------------- 时间窗口（纯函数，离线可测） ----------------
def _nth_sunday(year: int, month: int, n: int) -> date:
    """某月第 n 个周日。"""
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _la_offset(d: date) -> int:
    """洛杉矶偏移（分钟）：夏令时 -420（3月第2个周日 ~ 11月第1个周日），冬令时 -480。"""
    return -420 if _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1) else -480


def tz_today(timezone: str = DEFAULT_TZ, now_utc: Optional[datetime] = None) -> date:
    """目标时区的「今天」（切换日误差 ≤1 小时，只影响 --days 的边界日）。"""
    if timezone not in TZ_OFFSET_MIN:
        raise ValueError(f"未登记的时区 {timezone!r}；可用：{', '.join(TZ_OFFSET_MIN)}")
    u = now_utc or datetime.utcnow()
    off = _la_offset(u.date()) if timezone == "America/Los_Angeles" else TZ_OFFSET_MIN[timezone]
    return (u + timedelta(minutes=off)).date()


def _norm_day(s: str, flag: str) -> str:
    """YYYY-M-D → YYYY-MM-DD；空/非法抛 ValueError。"""
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{flag} 日期不能为空")
    try:
        return datetime.strptime(s, DATE_FMT).strftime(DATE_FMT)
    except ValueError:
        raise ValueError(f"{flag} 日期格式应为 YYYY-MM-DD，实际收到: {s!r}")


def resolve_window(days: int = DEFAULT_DAYS, begin: str = "", end: str = "",
                   date: str = "", timezone: str = DEFAULT_TZ,
                   today: Optional[date] = None) -> Tuple[str, str]:
    """推导 (begin, end) 闭区间；**默认近 days 天含今天**（与 fbs-delivery 的"默认不过滤"不同）。

    优先级：begin/end 成对 > date 单天 > days（默认 30）。只给 --end → ValueError（同 feishu-vc-stats）。
    """
    b_in, e_in, d_in = (begin or "").strip(), (end or "").strip(), (date or "").strip()
    if e_in and not (b_in or d_in):
        raise ValueError("--end 必须与 --begin 或 --date 同用（单独 --end 无法确定起点，已中止）")
    if b_in or e_in or d_in:
        b = _norm_day(b_in or d_in or e_in, "--begin/--date")
        e = _norm_day(e_in or d_in or b_in, "--end/--date")
        if b > e:
            b, e = e, b
        return b, e
    n = max(1, common.to_int(days, DEFAULT_DAYS))
    t = today or tz_today(timezone)
    return (t - timedelta(days=n - 1)).strftime(DATE_FMT), t.strftime(DATE_FMT)


def in_window(day: str, begin: str, end: str) -> bool:
    """day 是否落在闭区间 [begin,end]；begin/end 任一为空 → 恒 True（不过滤）。"""
    if not begin or not end:
        return True
    return bool(day) and begin <= day <= end


# ---------------- 解析与归类（纯函数） ----------------
def classify_status(status: Any) -> str:
    """状态 → 'return' / 'pending' / 'done' / 'unknown'（大小写不敏感；未知值不猜测）。"""
    s = str(status or "").strip().lower()
    if s in RETURN_STATUSES:
        return KIND_RETURN
    if s in PENDING_STATUSES:
        return KIND_PENDING
    if s in DONE_STATUSES:
        return KIND_DONE
    return KIND_UNKNOWN


def parse_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`data.orders[]` 一项 → 归一化明细行（缺字段容错，全部字符串化）。"""
    p = (raw or {}).get("product") or {}
    o = (raw or {}).get("order") or {}
    created = str(o.get("created") or "")
    oid = str(o.get("id") or "")
    st = str(o.get("status") or "").strip()
    return {
        "order_id": oid,
        "order_no": oid.rsplit(".", 2)[0] if oid.count(".") >= 2 else oid,
        "created": created, "created_day": created[:10],
        "updated": str(o.get("updated") or ""),
        "status": st, "kind": classify_status(st),
        "cancel_type": str(o.get("cancelType") or ""),
        "nm_id": str(p.get("id") or ""),
        "vc": str(p.get("vendorCode") or "").strip(),
        "title": str(p.get("name") or ""),
        "subject": str(p.get("subjectName") or ""),
        "size": str((o.get("size") or {}).get("title") or ""),
        "price": (o.get("price") or {}).get("seller"),
        "warehouse": str((o.get("warehouse") or {}).get("title") or ""),
        "dest": str((o.get("destination") or {}).get("title") or ""),
        "is_mp": bool(o.get("isMp")), "is_b2b": bool(o.get("isB2b")),
    }


# ---------------- 聚合 ----------------
def aggregate_by_cn(rows: Sequence[Dict[str, Any]],
                    cn_stock_sku: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """按「商品中文名」聚合（核心产出）。

    排序：`(是否以 "(" 开头, -订单数, 中文名)` —— `(中文名未知)` 等未归类桶恒置尾。
    `rate` / `rate_done` 无法计算（分母 ≤0）时为 None，由 xlsx 渲染成 "-"。
    """
    stat: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cn = r.get("cn") or NO_CN
        d = stat.setdefault(cn, {"vcs": set(), "shops": set(), "orders": 0,
                                 "returns": 0, "pending": 0, "unknown": 0})
        if r.get("vc"):
            d["vcs"].add(r["vc"])
        if r.get("shop_label"):
            d["shops"].add(r["shop_label"])
        d["orders"] += 1
        k = r.get("kind") or classify_status(r.get("status"))
        if k == KIND_RETURN:
            d["returns"] += 1
        elif k == KIND_PENDING:
            d["pending"] += 1
        elif k == KIND_UNKNOWN:
            d["unknown"] += 1
    sku_map = cn_stock_sku or {}
    out: List[Dict[str, Any]] = []
    for cn, d in stat.items():
        done = d["orders"] - d["pending"]
        out.append({
            "cn": cn, "stock_sku": sku_map.get(cn, ""),
            "orders": d["orders"], "returns": d["returns"], "pending": d["pending"],
            "unknown": d["unknown"], "vc_count": len(d["vcs"]), "shops": sorted(d["shops"]),
            "rate": (d["returns"] / d["orders"]) if d["orders"] else None,
            "rate_done": (d["returns"] / done) if done > 0 else None,
        })
    out.sort(key=lambda r: (str(r["cn"]).startswith("("), -r["orders"], r["cn"]))
    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out


def aggregate_by_shop(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按店铺汇总（供控制台与"每店 Sheet"行数校验）。"""
    stat: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        label = r.get("shop_label") or "(未知店铺)"
        d = stat.setdefault(label, {"orders": 0, "returns": 0, "pending": 0, "unknown": 0,
                                    "shop_id": r.get("shop_id")})
        d["orders"] += 1
        k = r.get("kind") or KIND_UNKNOWN
        if k == KIND_RETURN:
            d["returns"] += 1
        elif k == KIND_PENDING:
            d["pending"] += 1
        elif k == KIND_UNKNOWN:
            d["unknown"] += 1
    out = [{"shop_label": label, **d} for label, d in stat.items()]
    out.sort(key=lambda r: -r["orders"])
    return out


# ---------------- 采集入口 ----------------
def collect(days: int = DEFAULT_DAYS, begin: str = "", end: str = "", date: str = "",
            shops: Any = None, timezone: str = DEFAULT_TZ, page_size: int = feed.PAGE_SIZE,
            max_pages: int = feed.MAX_PAGES, limit: int = 0, with_cn: bool = True,
            local_filter: bool = True, interval: float = 0.3,
            verbose: bool = False) -> Dict[str, Any]:
    """★ 全流程（只读）：逐店（各自 cookie 会话）拉订单动态 → 过滤解析 → vc→中文名 → 聚合。

    接口 `currentPeriod` 实测按 **updated** 过滤（不是 created）⇒ 分两步：
      ① 服务端粗筛超集：请求窗口 [begin, max(end, 今天)]（历史区间须放宽到今天就够，
         因为 created ≤ end 的订单其 updated 只会更晚，放宽后不会漏）；
      ② 本地按 created_day ∈ [begin,end] 精确过滤，越界行计入 `out_of_window`（预期现象，非异常）。
    `local_filter=False` 时跳过 ②，此时口径 = 「窗口内有过状态变更的订单」（一般不要用）。

    返回 dict：{'begin','end','server_end','timezone','rows','per_shop','all_shops','by_cn',
                 'by_vc','by_cn_by_shop','by_shop','status_counter','unknown_status',
                 'out_of_window','dup_order_ids','failed_shops'}
    """
    cred = credentials.get()
    all_shops = cred.wb_shops()
    if not all_shops:
        raise RuntimeError("credentials.json 中没有已填 cookie 的店铺（请编辑 data/wb.shops）")
    pairs = select_shops(all_shops, shops or "")
    if not pairs:
        raise RuntimeError(f"指定店铺 {shops!r} 未在凭证中找到已填 cookie 的店铺")

    b, e = resolve_window(days=days, begin=begin, end=end, date=date, timezone=timezone)
    server_end = max(e, tz_today(timezone).strftime(DATE_FMT))   # 见 docstring ①

    resolve_cn = None
    if with_cn:
        try:
            resolve_cn, _ = MappingRepository.build_vc_resolver()
        except Exception as ex:
            print(f"  [警告] 中文名真源（映射表）加载失败（{type(ex).__name__}: {ex}）；中文名将留空")
    cn_stock_sku = load_cn_stock_sku()   # {中文名: 库存SKU}

    rows: List[Dict[str, Any]] = []
    per_shop: List[Dict[str, Any]] = []
    out_of_window = 0
    seen_ids: set = set()
    dup_ids: set = set()

    for shop in pairs:
        sid = common.to_int(shop.get("shopId"))
        name = str(shop.get("shopName") or sid)
        label = f"{name}({sid})"
        cnt: Counter = Counter()
        try:
            session = wb_api.make_session(shop, cred.root_version)
            for raw in feed.iter_orders(session, b, server_end, limit=page_size, max_pages=max_pages,
                                        timezone=timezone, max_rows=limit, sleep=interval,
                                        verbose=verbose):
                r = parse_row(raw)
                if local_filter and not in_window(r["created_day"], b, e):
                    out_of_window += 1
                    continue
                r["cn"] = resolve_cn(r["vc"], "") if resolve_cn else ""
                r["stock_sku"] = cn_stock_sku.get(r["cn"], "") if r["cn"] else ""
                r["shop_name"], r["shop_id"], r["shop_label"] = name, sid, label
                if r["order_id"]:
                    if r["order_id"] in seen_ids:
                        dup_ids.add(r["order_id"])
                    seen_ids.add(r["order_id"])
                cnt[r["kind"]] += 1
                rows.append(r)
        except common.CookieExpiredError as ex:
            print(f"  [警告] 店铺 {name}: {ex}（该店中止，继续下一店；cookie 失效请跑 wb.py cookies-update）")
            per_shop.append({"shop": name, "shop_id": sid, "label": label, "orders": 0,
                             "returns": 0, "pending": 0, "unknown": 0, "error": str(ex)[:160]})
            continue
        except Exception as ex:
            print(f"  [警告] 店铺 {name} 处理失败: {ex}（该店中止，继续下一店）")
            per_shop.append({"shop": name, "shop_id": sid, "label": label, "orders": 0,
                             "returns": 0, "pending": 0, "unknown": 0, "error": str(ex)[:160]})
            continue
        n = sum(cnt.values())
        per_shop.append({"shop": name, "shop_id": sid, "label": label, "orders": n,
                         "returns": cnt[KIND_RETURN], "pending": cnt[KIND_PENDING],
                         "unknown": cnt[KIND_UNKNOWN], "error": ""})
        print(f"  === 店铺 {label} ===  明细 {n} 行（退货 {cnt[KIND_RETURN]} / "
              f"未完结 {cnt[KIND_PENDING]} / 未知 {cnt[KIND_UNKNOWN]}）")
        time.sleep(SHOP_SLEEP)

    return {
        "begin": b, "end": e, "server_end": server_end, "timezone": timezone,
        "rows": rows, "per_shop": per_shop, "all_shops": pairs,
        "by_cn": aggregate_by_cn(rows, cn_stock_sku),
        "by_vc": aggregate_by_vc(rows, cn_stock_sku),
        "by_cn_by_shop": {p["label"]: aggregate_by_cn(
            [x for x in rows if x["shop_label"] == p["label"]], cn_stock_sku)
            for p in per_shop},
        "by_shop": aggregate_by_shop(rows),
        "status_counter": Counter(r["status"] or "(空)" for r in rows),
        "unknown_status": Counter(r["status"] for r in rows if r["kind"] == KIND_UNKNOWN),
        "out_of_window": out_of_window,
        "dup_order_ids": sorted(dup_ids),
        "failed_shops": [p for p in per_shop if p.get("error")],
    }


def order_stats(**kwargs) -> Dict[str, Any]:
    """★ 库入口（供其它脚本 import）：WB「订单动态」按商品中文名统计（只读）。参数见 `collect`。"""
    return collect(**kwargs)


# ---------------- CLI 入口 ----------------
def run(args: Any) -> int:
    """`wb.py order-stats`：WB「订单动态」近一月订单按商品中文名统计（只读，输出 xlsx 多 Sheet）。"""
    common.ensure_utf8_stdout()
    days = getattr(args, "days", DEFAULT_DAYS)
    timezone = getattr(args, "timezone", DEFAULT_TZ) or DEFAULT_TZ
    shops = getattr(args, "shops", "") or ""
    top = common.to_int(getattr(args, "top", 0))
    try:
        b, e = resolve_window(days=days, begin=getattr(args, "begin", ""),
                              end=getattr(args, "end", ""), date=getattr(args, "date", ""),
                              timezone=timezone)
    except ValueError as err:
        print(f"[错误] {err}")
        return 1

    print(f"[区间] {b} ~ {e}（按**下单时间 created** 统计；时区 {timezone}，比北京时间晚约 15 小时）"
          f"｜店铺={shops or '全部'}"
          f"｜中文名={'不解析' if getattr(args, 'no_cn', False) else '解析'}")
    print("[口径] 接口 currentPeriod 按 updated 粗筛（超集），本地再按 created 精筛；"
          "订单数=明细行数（一行=一件）")
    print(f"[环境] root-version={credentials.get().root_version}（与抓包不一致时若报 400/403，"
          f"请更新 data/credentials.json）")

    try:
        r = collect(days=days, begin=getattr(args, "begin", ""), end=getattr(args, "end", ""),
                    date=getattr(args, "date", ""), shops=shops, timezone=timezone,
                    page_size=common.to_int(getattr(args, "page_size", feed.PAGE_SIZE)) or feed.PAGE_SIZE,
                    max_pages=common.to_int(getattr(args, "max_pages", feed.MAX_PAGES)) or feed.MAX_PAGES,
                    limit=common.to_int(getattr(args, "limit", 0)),
                    with_cn=not getattr(args, "no_cn", False),
                    local_filter=not getattr(args, "no_local_filter", False),
                    interval=float(getattr(args, "interval", 0.3) or 0),
                    verbose=not getattr(args, "quiet", False))
    except (RuntimeError, ValueError) as err:
        print(f"[错误] {err}")
        return 1

    rows, by_cn = r["rows"], r["by_cn"]
    failed = r["failed_shops"]
    if failed:
        print(f"  [警告] {len(failed)}/{len(r['per_shop'])} 家店铺失败，结果不完整："
              + "、".join(p["label"] for p in failed))
    if not rows:
        print("\n窗口内没有命中任何订单行（检查 --shops/--days/--begin/--end）")
        return 1 if len(failed) == len(r["per_shop"]) else 0

    print(f"\n[汇总] 订单行 {len(rows)} 行｜去重订单号 {len({x['order_no'] for x in rows if x['order_no']})} 个"
          f"｜中文名 {len(by_cn)} 种｜去重供应商代码 {len({x['vc'] for x in rows if x['vc']})} 个")
    print("[状态分布] " + "｜".join(f"{k}={v}" for k, v in r["status_counter"].most_common()))
    if r["unknown_status"]:
        print("  [警告] 未知订单状态（已计入订单数，未归类）："
              + "｜".join(f"{k}={v}" for k, v in r["unknown_status"].most_common()))
    if r["out_of_window"]:
        print(f"  [提示] 接口按 updated 粗筛，另有 {r['out_of_window']} 行的下单日期在窗口外，"
              f"已按 created 剔除（正常现象，非异常）")
    if r["dup_order_ids"]:
        print(f"  [警告] {len(r['dup_order_ids'])} 个订单号跨页重复出现（翻页期间状态变化导致错页，"
              f"可能 ±数行差异）")
    unk = next((x for x in by_cn if x["cn"] == NO_CN), None)
    if unk:
        print(f"  [警告] 中文名未解析 {unk['orders']} 行（{unk['orders'] / len(rows):.2%}），"
              f"归入 {NO_CN} 桶；请检查映射表/前缀码")

    print(f"\n  序号 | 商品中文名 | 订单数 | 退货数 | 退货率 | 完单口径 | 未完结 | 供应商代码数 | 库存SKU")
    shown = by_cn[:top] if top else by_cn
    for x in shown:
        rd = f"{x['rate_done']:.2%}" if x["rate_done"] is not None else "-"
        rt = f"{x['rate']:.2%}" if x["rate"] is not None else "-"
        print(f"  {x['rank']:>3} | {x['cn']} | {x['orders']} | {x['returns']} | {rt} | {rd} | "
              f"{x['pending']} | {x['vc_count']} | {x.get('stock_sku') or '-'}")
    if top and len(by_cn) > top:
        print(f"  ... 其余 {len(by_cn) - top} 个中文名见 xlsx")

    print_vc_table(r["by_vc"], top)

    try:
        from .order_stats_xlsx import write_xlsx
        path = write_xlsx(r, out=getattr(args, "out", "") or "",
                          with_detail=not getattr(args, "no_detail", False),
                          detail_max_rows=common.to_int(getattr(args, "detail_max_rows", 0)))
    except ImportError as ex:
        print(f"[错误] 生成 xlsx 失败：{ex}（请用带 openpyxl 的 venv Python 运行）")
        return 1
    print(f"\n[xlsx] 汇总 {len(by_cn)} 行 / 供应商代码 {len(r['by_vc'])} 个 / "
          f"店铺 {len(r['by_shop'])} 家 / 明细 {len(rows)} 行 → "
          f"{path or '（未生成，见上方警告）'}")
    return 1 if failed and len(failed) == len(r["per_shop"]) else 0

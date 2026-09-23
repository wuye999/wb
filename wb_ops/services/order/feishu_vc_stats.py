# -*- coding: utf-8 -*-
"""
wb_ops 飞书「订单登记」按供应商代码(BCS编号)统计单数并降序（只读）

用途：读飞书多维表格「订单登记」表在指定时间窗口内的记录，按**供应商代码
（`BCS编号` 列 = vendorCode）跨店合并**聚合「单数」，按单数降序输出到控制台与 CSV。

口径（2026-09-23 与用户确认）：
- **单数 = 登记记录条数**（一条记录 = 一单中的一个商品）；另输出「件数」= `订单量` 列求和作副列。
- 同一 vendorCode 在不同店铺的 `wb编号`(nmId) 不同，但**不论哪家店都累加进同一条**；
  表内「店铺」列只用于过滤与展示，不参与分组。
- 时间基准列 = `日期`（= 付款时间）；窗口为**闭区间** [begin, end]。
- 默认近 **7 天含今天**：begin = 今天-6、end = 今天。

数据源与鉴权：飞书多维表格，经 `lark-cli`（`--as user`，无需另配 token）读取。复用：
- `services/order/feishu_register.resolve_base / resolve_table`（地址与表名 → token/id）
- `services/order/mabang_stock._record_list_all / _sv`（**分页**读全表，防 ndjson 2000 条截断）

可编程调用（供其它脚本 import；全部显式关键字参数）：
    from wb_ops.services.order.feishu_vc_stats import vc_order_stats
    r = vc_order_stats(days=7, shops=["9352"], by_prefix=False)
    for row in r["rows"]:
        print(row["rank"], row["key"], row["orders"], row["qty"], row["cn"])

纯只读：不写入飞书任何表，不调马帮接口，不做任何写后验证。
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
from wb_ops.storage.mapping_repo import MappingRepository
from .feishu_register import resolve_base, resolve_table
from .mabang_stock import _record_list_all, _sv

DATE_FMT = "%Y-%m-%d"
DEFAULT_TABLE = "订单登记"
NO_VC_KEY = "(无BCS编号)"          # BCS编号 为空的记录单独成桶，不静默丢弃
NO_PREFIX_KEY = "(无法识别前缀)"     # --by-prefix 时 vc 不匹配 VC_PREFIX_RE
CSV_FIELDS = ["排名", "BCS编号", "商品中文名", "单数", "件数", "涉及店铺"]

# 「订单登记」表字段名（实测 2026-09-23）
FIELD_DATE = "日期"          # datetime，= 付款时间 → 时间基准列
FIELD_ORDER_ID = "订单编号"   # 平台单号
FIELD_SHOP = "店铺"          # select 多选（璧山1/2/3/5/10、袁州1/2/3）
FIELD_VC = "BCS编号"         # = vendorCode（用户所说的「供应商代码」）
FIELD_CN = "商品中文名"
FIELD_QTY = "订单量"         # number，件数


# ---------------- 纯函数（离线可测，不触网） ----------------
def _to_int(v: Any, default: int = 1) -> int:
    """尽力转 int（'3' → 3、3.0 → 3、失败返回 default）"""
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def parse_row(fields: Dict[str, Any]) -> Dict[str, Any]:
    """飞书一行 `fields` → 归一化行。

    返回 {'日期': 'YYYY-MM-DD' 或 '', 'vc', 'cn', 'shops': [短名...], 'qty': int, 'order_id'}
    取值统一经 `_sv()`（兼容 list/dict 富文本与 number）。
    """
    day = _sv(fields.get(FIELD_DATE))[:10]
    raw_shops = fields.get(FIELD_SHOP)
    if isinstance(raw_shops, list):
        shops = [_sv(x) for x in raw_shops]
    else:
        shops = [_sv(raw_shops)] if _sv(raw_shops) else []
    return {
        "日期": day,
        "vc": _sv(fields.get(FIELD_VC)),
        "cn": _sv(fields.get(FIELD_CN)),
        "shops": [s for s in shops if s],
        "qty": _to_int(fields.get(FIELD_QTY), 1),
        "order_id": _sv(fields.get(FIELD_ORDER_ID)),
    }


def in_window(row: Dict[str, Any], begin: str, end: str) -> bool:
    """是否落在闭区间 [begin, end]（ISO 日期串可直接比较）；无日期的行不命中"""
    day = row.get("日期") or ""
    if not day or not begin or not end:
        return False
    return begin <= day <= end


def match_shops(row: Dict[str, Any], shop_labels: Optional[Set[str]]) -> bool:
    """店铺过滤：shop_labels 为空/None = 不过滤；否则要求店铺列与标签集有交集"""
    if not shop_labels:
        return True
    return bool(set(row.get("shops") or []) & set(shop_labels))


def vc_prefix(vc: str) -> str:
    """完整 vendorCode → 4 位前缀码（不匹配 config.VC_PREFIX_RE 时返回 NO_PREFIX_KEY）"""
    m = re.match(config.VC_PREFIX_RE, vc or "")
    return m.group(1) if m else NO_PREFIX_KEY


def aggregate(rows: Sequence[Dict[str, Any]], by_prefix: bool = False) -> List[Dict[str, Any]]:
    """按聚合键分组计数。

    key = 完整 vendorCode（默认）或 4 位前缀码（by_prefix=True）；
    BCS编号 为空的记录归入 NO_VC_KEY 桶（不丢弃）。
    排序：单数降序 → 同数按 key 升序；未归类桶（`(无BCS编号)` / `(无法识别前缀)`）恒排最后。
    行内含 {'rank','key','cn','orders','qty','shops'}，
    其中 `shops` 为该 key 下各店单数降序的店铺短名列表。
    """
    orders: Counter = Counter()
    qty: Counter = Counter()
    shop_cnt: Dict[str, Counter] = defaultdict(Counter)
    cn_map: Dict[str, str] = {}
    for r in rows:
        if by_prefix:
            key = vc_prefix(r.get("vc") or "")
        else:
            key = r.get("vc") or NO_VC_KEY
        orders[key] += 1
        qty[key] += r.get("qty") or 1
        for s in (r.get("shops") or []):
            shop_cnt[key][s] += 1
        if not by_prefix and r.get("cn") and key not in cn_map:
            cn_map[key] = r["cn"]

    out: List[Dict[str, Any]] = []
    # 排序：单数降序 → 同数按 key 升序；`(无BCS编号)`/`(无法识别前缀)` 这类未归类桶**恒定排在最后**
    # （否则 '(' 的码位小于 'B' 会让它们插在真实 vendorCode 前面，干扰阅读）
    for key, n in sorted(orders.items(),
                         key=lambda kv: (str(kv[0]).startswith("("), -kv[1], kv[0])):
        out.append({
            "key": key,
            "cn": "" if by_prefix else cn_map.get(key, ""),
            "orders": n,
            "qty": qty[key],
            "shops": [s for s, _ in sorted(shop_cnt[key].items(), key=lambda kv: (-kv[1], kv[0]))],
        })
    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out


def resolve_window(days: int = 7, begin: str = "", end: str = "", date: str = "") -> Tuple[str, str]:
    """推导时间窗口（闭区间）。

    优先级：`begin`/`end` 成对 > `date` 单天 > `days` 近 N 天（含今天，默认 7）。
    只给 `--begin` → 按单天处理；只给 `--end` → ValueError（与 mabang-stock-daily 同规矩）。
    """
    begin = (begin or "").strip()
    end = (end or "").strip()
    date = (date or "").strip()
    if end and not (begin or date):
        raise ValueError("--end 必须与 --begin 或 --date 同用（单独 --end 无法确定起点，已中止）")
    if begin or end or date:
        b = _norm_day(begin or date or end, "--begin/--date")
        e = _norm_day(end or date or begin, "--end/--date")
    else:
        n = common.to_int(days, 7)
        if n < 1:
            raise ValueError(f"--days 必须 >= 1，实际收到 {days!r}")
        today = datetime.now().date()
        b = (today - timedelta(days=n - 1)).strftime(DATE_FMT)
        e = today.strftime(DATE_FMT)
    if b > e:
        b, e = e, b
    return b, e


def _norm_day(s: str, flag: str) -> str:
    """YYYY-M-D → YYYY-MM-DD（strptime 自动补零）；空/非法抛 ValueError"""
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{flag} 日期不能为空")
    try:
        return datetime.strptime(s, DATE_FMT).strftime(DATE_FMT)
    except ValueError:
        raise ValueError(f"{flag} 日期格式应为 YYYY-MM-DD，实际收到: {s!r}")


def resolve_shop_labels(tokens: Any) -> Set[str]:
    """店铺过滤参数 → 店铺短名集合。

    '9352' / '袁州1' / ['9352','袁州3'] / '9352,袁州3' 均可；
    数字优先经 `credentials.wb_shops()` 的 shopId→shopName 反查（9352→袁州1），
    非数字原样保留（可写 璧山1 这类不在本凭证里的店铺短名）；
    无法解析的数字 → 打 [警告] 并保留原值（宁可匹配不到，也不静默扩大范围）。
    """
    if not tokens:
        return set()
    if isinstance(tokens, str):
        items = [t for t in re.split(r"[,\s]+", tokens) if t]
    elif isinstance(tokens, (list, tuple, set)):
        items = [str(t).strip() for t in tokens if str(t).strip()]
    else:
        items = [str(tokens).strip()]

    sid2name: Dict[int, str] = {}
    try:
        for s in credentials.get().wb_shops():
            name = s.get("shopName") or ""
            if name:
                sid2name[common.to_int(s.get("shopId"))] = name
    except Exception as e:
        print(f"  [警告] 读取 WB 店铺名失败（{type(e).__name__}: {e}）；数字店铺ID 将按原值匹配")

    labels: Set[str] = set()
    for t in items:
        if t.isdigit():
            name = sid2name.get(int(t))
            if name:
                labels.add(name)
            else:
                print(f"  [警告] 店铺ID {t} 未在凭证中找到对应店铺短名，将按原值匹配（可能匹配不到）")
                labels.add(t)
        else:
            labels.add(t)
    return labels


# ---------------- 读表 ----------------
def read_register_rows(base_token: str, table_id: str, begin: str = "", end: str = "",
                       shop_labels: Optional[Set[str]] = None
                       ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """读「订单登记」全量记录 → 本地过滤 → (命中行, 统计)。

    复用 `mabang_stock._record_list_all`（分页，防 ndjson 2000 条截断）。
    `stats` 显式记录各类未命中原因，**不静默丢弃**：
      {'总记录','无日期','日期不在窗口','店铺被过滤','无BCS编号'}
    （`无BCS编号` 的记录**仍保留**，会归入 `(无BCS编号)` 桶参与统计。）
    """
    rows: List[Dict[str, Any]] = []
    stats = {"总记录": 0, "无日期": 0, "日期不在窗口": 0, "店铺被过滤": 0, "无BCS编号": 0}
    for d in _record_list_all(base_token, table_id):
        fields = d.get("fields") or d
        stats["总记录"] += 1
        row = parse_row(fields)
        if not row["日期"]:
            stats["无日期"] += 1
            continue
        if begin and end and not in_window(row, begin, end):
            stats["日期不在窗口"] += 1
            continue
        if not match_shops(row, shop_labels):
            stats["店铺被过滤"] += 1
            continue
        if not row["vc"]:
            stats["无BCS编号"] += 1
        rows.append(row)
    return rows, stats


def _resolve_missing_cn(agg_rows: List[Dict[str, Any]]) -> int:
    """用本地映射真源补全缺失的中文名（仅确有缺失时才读映射表）；返回补齐条数"""
    todo = [r for r in agg_rows if not r["cn"] and not str(r["key"]).startswith("(")]
    if not todo:
        return 0
    try:
        resolve_cn, _ = MappingRepository.build_vc_resolver()
    except Exception as e:
        print(f"  [警告] 中文名真源（映射表）加载失败（{type(e).__name__}: {e}）；缺失中文名将留空")
        return 0
    n = 0
    for r in todo:
        cn = resolve_cn(r["key"], "")
        if cn:
            r["cn"] = cn
            n += 1
    return n


# ---------------- 聚合入口 ----------------
def collect_stats(begin: str, end: str, shops: Any = None, url: str = "", table: str = DEFAULT_TABLE,
                   by_prefix: bool = False, with_cn: bool = True,
                   shop_labels: Optional[Set[str]] = None) -> Dict[str, Any]:
    """按**已确定**的窗口读表并聚合（不含窗口推导，供 `run` 与 `vc_order_stats` 共用）。

    `shop_labels` 已解析时直接传入（避免重复解析与重复告警），否则由 `shops` 解析。

    返回 dict：{'begin','end','table','base_token','table_id','shop_labels','stats',
                 'total_orders','total_qty','unique_order_ids','vc_count','rows','raw_rows'}
    失败抛 RuntimeError/ValueError，交由调用方处理。
    """
    url = (url or "").strip() or credentials.get().feishu_base_url()
    if not url:
        raise RuntimeError("未提供飞书表格地址且 credentials.json 的 feishu.base_url 缺失")
    labels = resolve_shop_labels(shops) if shop_labels is None else set(shop_labels)
    base_token = resolve_base(url)
    table_id = resolve_table(base_token, table)
    rows, stats = read_register_rows(base_token, table_id, begin, end, labels or None)
    agg = aggregate(rows, by_prefix=by_prefix)
    if with_cn and not by_prefix:
        _resolve_missing_cn(agg)
    return {
        "begin": begin, "end": end, "table": table,
        "base_token": base_token, "table_id": table_id,
        "shop_labels": sorted(labels),
        "stats": stats,
        "total_orders": len(rows),
        "total_qty": sum(r.get("qty") or 1 for r in rows),
        "unique_order_ids": len({r["order_id"] for r in rows if r.get("order_id")}),
        "vc_count": len([r for r in agg if not str(r["key"]).startswith("(")]),
        "rows": agg,
        "raw_rows": rows,
    }


def vc_order_stats(days: int = 7, begin: str = "", end: str = "", date: str = "", shops: Any = None,
                   url: str = "", table: str = DEFAULT_TABLE, by_prefix: bool = False,
                   with_cn: bool = True) -> Dict[str, Any]:
    """★ 库入口（供其它脚本 import）：飞书「订单登记」按供应商代码统计单数（降序）。

    参数（全部显式关键字）：
      days       近 N 天（含今天，默认 7）；仅当 begin/end/date 均未给时生效
      begin/end  自定义闭区间 YYYY-MM-DD（只给 end 报 ValueError；只给 begin 按单天）
      date       单天 YYYY-MM-DD（等价 begin=end）
      shops      店铺过滤：'9352' / '袁州1' / ['9352','袁州3']（None/空 = 全部店铺）
      url        飞书表格地址（默认读 credentials 的 feishu.base_url）
      table      表名（默认「订单登记」）
      by_prefix  按 vendorCode 的 4 位前缀码聚合（默认按完整 vendorCode）
      with_cn    是否用本地映射真源补全缺失的中文名

    返回 `collect_stats(...)` 的 dict（见其 docstring）。
    """
    b, e = resolve_window(days=days, begin=begin, end=end, date=date)
    return collect_stats(b, e, shops=shops, url=url, table=table,
                         by_prefix=by_prefix, with_cn=with_cn)


def write_csv(agg_rows: Sequence[Dict[str, Any]]) -> str:
    """聚合结果 → CSV（全量，utf-8-sig）；返回路径"""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"飞书订单按供应商代码统计_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in agg_rows:
            w.writerow({
                "排名": r["rank"],
                "BCS编号": r["key"],
                "商品中文名": r["cn"],
                "单数": r["orders"],
                "件数": r["qty"],
                "涉及店铺": "、".join(r["shops"]),
            })
    return path


# ---------------- CLI 入口 ----------------
def run(args: Any) -> int:
    """`wb.py feishu-vc-stats`：读飞书「订单登记」→ 按供应商代码统计单数降序（只读）。"""
    common.ensure_utf8_stdout()
    try:
        begin, end = resolve_window(days=getattr(args, "days", 7), begin=getattr(args, "begin", ""),
                                    end=getattr(args, "end", ""), date=getattr(args, "date", ""))
    except ValueError as e:
        print(f"[错误] {e}")
        return 1

    shops = getattr(args, "shops", "") or ""
    table = (getattr(args, "table", "") or "").strip() or DEFAULT_TABLE
    by_prefix = bool(getattr(args, "by_prefix", False))
    with_cn = not bool(getattr(args, "no_cn", False))
    top = common.to_int(getattr(args, "top", 0))
    labels = resolve_shop_labels(shops)

    print(f"[区间] {begin} ~ {end}（闭区间；基准列={FIELD_DATE}/付款时间）"
          f"｜店铺={'、'.join(sorted(labels)) if labels else '全部'}"
          f"｜口径={'4位前缀码' if by_prefix else '完整供应商代码'}")
    if not (getattr(args, "begin", "") and (getattr(args, "end", "") or getattr(args, "date", ""))):
        if getattr(args, "begin", ""):
            print(f"[提示] 仅给 --begin，按单天 {begin} 处理（如需区间请补 --end）")

    try:
        r = collect_stats(begin, end, url=getattr(args, "url", ""), table=table,
                          by_prefix=by_prefix, with_cn=with_cn, shop_labels=labels)
    except (RuntimeError, ValueError) as e:
        print(f"[错误] {str(e)[:300]}")
        return 1

    stats = r["stats"]
    unit = "4位前缀码" if by_prefix else "供应商代码"
    print(f"[表格] base_token={r['base_token']} table_id={r['table_id']}（{r['table']}）")
    print(f"[汇总] 表内 {stats['总记录']} 条记录 → 命中 {r['total_orders']} 条"
          f"（件数 {r['total_qty']}）｜去重订单编号 {r['unique_order_ids']} 个"
          f"｜{unit} {r['vc_count']} 个")
    dropped = " / ".join(f"{k}={v}" for k, v in stats.items() if k != "总记录" and v)
    print(f"[明细] 未命中原因（显式标注）: {dropped or '无'}")

    rows = r["rows"]
    if not rows:
        print("\n该窗口内没有命中的登记记录（检查 --days/--begin/--end 或 --shops）")
        return 0

    print(f"\n  排名 | {'供应商代码' if not by_prefix else '前缀码'} | 商品中文名 | 单数 | 件数 | 涉及店铺")
    shown = rows[:top] if top else rows
    for row in shown:
        print(f"  {row['rank']:>3} | {row['key']} | {row['cn'] or '-'} | {row['orders']} | "
              f"{row['qty']} | {'、'.join(row['shops']) or '-'}")
    if top and len(rows) > top:
        print(f"  ... 其余 {len(rows) - top} 个供应商代码见 CSV")

    keys = [row["key"] for row in rows if not str(row["key"]).startswith("(")]
    if keys:
        print(f"\n[供应商代码] 按单数降序（英文逗号分隔，可直接复制）：")
        print(",".join(keys))

    path = write_csv(rows)
    print(f"\n[日志] 聚合 {len(rows)} 行 → {path}")
    return 0

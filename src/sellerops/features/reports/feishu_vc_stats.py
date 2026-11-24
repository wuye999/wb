"""飞书「订单登记」读取，供推广审计与商品综合报告复用。"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from sellerops.core import utils as common
from sellerops.features.orders.mabang_stock import _sv
from sellerops.integrations.feishu.tables import list_records, resolve_base, resolve_table
from sellerops.settings import credentials


DATE_FMT = "%Y-%m-%d"
DEFAULT_TABLE = "订单登记"

FIELD_DATE = "日期"
FIELD_ORDER_ID = "订单编号"
FIELD_SHOP = "店铺"
FIELD_VC = "BCS编号"
FIELD_CN = "商品中文名"
FIELD_QTY = "订单量"


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


def read_register_rows(base_token: str, table_id: str, begin: str, end: str
                       ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """分页读取「订单登记」，按付款日期筛选并保留缺少 BCS 编号的记录。"""
    rows: List[Dict[str, Any]] = []
    stats = {"总记录": 0, "无日期": 0, "日期不在窗口": 0, "店铺被过滤": 0, "无BCS编号": 0}
    for record in list_records(base_token, table_id):
        stats["总记录"] += 1
        row = parse_row(record.get("fields") or record)
        if not row["日期"]:
            stats["无日期"] += 1
            continue
        if not in_window(row, begin, end):
            stats["日期不在窗口"] += 1
            continue
        if not row["vc"]:
            stats["无BCS编号"] += 1
        rows.append(row)
    return rows, stats


def vc_order_stats(days: int = 7, begin: str = "", end: str = "", date: str = "",
                   url: str = "", table: str = DEFAULT_TABLE) -> Dict[str, Any]:
    """读取推广审计使用的飞书订单行及筛选计数。"""
    begin, end = resolve_window(days=days, begin=begin, end=end, date=date)
    url = (url or "").strip() or credentials.get().feishu_base_url()
    if not url:
        raise RuntimeError("未提供飞书表格地址且 credentials.json 的 feishu.base_url 缺失")
    base_token = resolve_base(url)
    table_id = resolve_table(base_token, table)
    rows, stats = read_register_rows(base_token, table_id, begin, end)
    return {"begin": begin, "end": end, "raw_rows": rows, "stats": stats}

"""按列名解析商品价格表与映射报表的表头。"""
import re
import sys

COL_CN = "产品中文名"
COL_VC = "vendorCode"
COL_MIN_PRICE = "最低售价"
COL_TITLE = "俄文标题"
COL_IMG = "主图链接"
COL_L = "尺寸长(cm)"
COL_W = "尺寸宽(cm)"
COL_H = "尺寸高(cm)"
COL_WT = "毛重(kg)"
COL_NM = "WB商品码"
COL_SHOP_CNY_RE = r"^店铺\d+价格\(CNY\)$"

_WARNED = set()


class HeaderError(RuntimeError):
    """表头缺列 / 无法定位必需列。"""


def read_headers(ws):
    """第一个工作表首行 → 去首尾空白的字符串列表。"""
    return [str(c.value or "").strip() for c in ws[1]]


def find_col(headers, name):
    """精确匹配（已 strip）→ 列索引；未命中 None。"""
    try:
        return headers.index(name)
    except ValueError:
        return None


def find_col_re(headers, pattern):
    """正则匹配 → 首个命中的列索引；未命中 None。"""
    rx = re.compile(pattern)
    return next((i for i, h in enumerate(headers) if rx.match(h)), None)


def _headers_repr(headers, limit=20):
    shown = " / ".join(h or "(空)" for h in headers[:limit])
    return shown + ("…" if len(headers) > limit else "")


def require_col(headers, name, table, path=None):
    """必需列定位失败时报告表名、路径、缺列和当前表头。"""
    idx = find_col(headers, name)
    if idx is not None:
        return idx
    where = f"{table}（{path}）" if path else table
    raise HeaderError(
        f"[表头错误] {where} 缺少必需列「{name}」；实际表头：{_headers_repr(headers)}")


def warn_missing(table, path, headers, name, hint=""):
    """可降级读取点：缺列时告警一次（同 表+文件+列 去重），由调用方把值置空。"""
    key = (table, str(path), name)
    if key in _WARNED:
        return
    _WARNED.add(key)
    tail = f"；{hint}" if hint else ""
    msg = (f"[警告] {table}（{path}）缺少列「{name}」{tail}；"
           f"实际表头：{_headers_repr(headers)}")
    print(msg, file=sys.stderr)


def reset_warnings():
    """清空告警去重缓存（测试隔离用）。"""
    _WARNED.clear()

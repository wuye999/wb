"""按表头读取商品价格表。最低售价公式求值失败时使用 Excel 缓存并告警。"""
import ast
import math
import operator
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

from sellerops.persistence.headers import COL_CN
from sellerops.persistence.headers import COL_MIN_PRICE
from sellerops.persistence.headers import find_col
from sellerops.persistence.headers import read_headers
from sellerops.persistence.headers import require_col

# 单元格引用：F2 / $F$2 / F$2 / $F2（前后不能紧邻字母数字，避免误伤函数名/数字）
_CELL_RE = re.compile(r"(?<![A-Z0-9_])\$?([A-Z]{1,3})\$?(\d{1,7})(?![0-9A-Z])")
# ROUND(表达式, 位数) —— 仅支持逗号分隔
_ROUND_RE = re.compile(r"^ROUND\((.+),\s*(\d+)\)$")
# 替换单元格引用后的安全表达式白名单
_SAFE_RE = re.compile(r"^[0-9eE+\-*/(). ]+$")
# 函数调用（SUM(...) / IF(...) 等）——出现即不支持，交给缓存兜底；普通括号不拦
_FUNC_RE = re.compile(r"[A-Z_][A-Z0-9_.]*\s*\(")

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}

# 商品价格表（PRICE_BOOK_XLSX）列名 → 逻辑字段（按表头名定位，不依赖列位）
BOSS_COLUMNS = {
    "img": "产品图片",
    "sku": "卖家SKU",
    "cn": COL_CN,
    "dim": "尺寸",
    "price": COL_MIN_PRICE,
    "prefix": "vendorCode前缀码",
    "stock_sku": "马帮库存SKU",
    "shipping_mode": "发货方式",
}

SHIPPING_MODE_MABANG = "马帮发货"
SHIPPING_MODE_WB = "WB发货"
SHIPPING_MODE_SKIP = "不处理"
VALID_SHIPPING_MODES = (SHIPPING_MODE_MABANG, SHIPPING_MODE_WB, SHIPPING_MODE_SKIP)


def price_book_columns(ws, path: str, required=("cn", "price")):
    """解析商品价格表表头 → {逻辑字段: 列索引}；required 缺列抛 HeaderError。

    列位可任意调整（用户会插列/挪列），一律按表头名定位。
    """
    headers = read_headers(ws)
    cols = {}
    for key, name in BOSS_COLUMNS.items():
        cols[key] = (require_col(headers, name, "商品价格表", path)
                     if key in required else find_col(headers, name))
    return cols


def to_number(v: Any) -> Optional[float]:
    """数值 / 数字文本 → float；None、bool、中文文本等 → None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _safe_eval(expr: str) -> Optional[float]:
    """只对「数字 + - * / ( )」求值（AST 白名单，不执行任意代码）。"""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and not isinstance(node.value, bool) \
                and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("unsupported node")

    try:
        return float(ev(ast.parse(expr, mode="eval")))
    except Exception:                      # noqa: BLE001 —— 语法/类型问题统一视为不可解析
        return None


def resolve_formula(formula: Any, ws, depth: int = 2) -> Optional[float]:
    """将支持的公式转换为数值，无法求值时交给调用方读取缓存。"""
    if not isinstance(formula, str):
        return to_number(formula)
    s = formula.strip()
    if not s.startswith("="):
        return to_number(s)
    s = s[1:].strip().upper().replace(" ", "").replace("_XLFN.", "")
    for _ in range(2):                     # 剥 ROUND(expr, n) 外壳
        m = _ROUND_RE.match(s)
        if not m:
            break
        s = m.group(1)
    # 只允许括号参与运算；出现「函数名(」式调用、跨表引用、区间 → 交给缓存兜底
    if _FUNC_RE.search(s) or "!" in s or ":" in s:
        return None

    from openpyxl.utils import column_index_from_string

    missing: List[str] = []

    def repl(m):
        col = column_index_from_string(m.group(1))
        val = ws.cell(row=int(m.group(2)), column=col).value
        num = to_number(val)
        if num is None and depth > 1 and isinstance(val, str) and val.strip().startswith("="):
            num = resolve_formula(val, ws, depth - 1)   # 引用单元格本身是公式
        if num is None:
            missing.append(m.group(0))
            return "0"
        return repr(num)

    expr = _CELL_RE.sub(repl, s)
    if missing or not _SAFE_RE.match(expr):
        return None
    return _safe_eval(expr)


def floor_price(v: Any) -> Optional[int]:
    """向下取整，空值和非有限数返回 None。"""
    num = to_number(v)
    if num is None or math.isnan(num) or math.isinf(num):
        return None
    return math.floor(num)


def read_price_book(path: str, *, require_price: bool = True) -> List[Dict[str, Any]]:
    """读取商品资料。默认要求中文名和最低售价表头。"""
    if not os.path.exists(path):
        return []
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=False)
    cached = None

    def cached_value(row, col):
        nonlocal cached
        if cached is None:
            cached = openpyxl.load_workbook(path, data_only=True)
        return cached["Sheet1"].cell(row=row, column=col + 1).value

    def val(row, col):
        return row[col].value if col is not None and len(row) > col else None

    try:
        ws = wb["Sheet1"]
        required = ("cn", "price") if require_price else ("cn",)
        cols = price_book_columns(ws, path, required=required)
        i_img, i_sku, i_cn = cols["img"], cols["sku"], cols["cn"]
        i_price, i_dim = cols["price"], cols["dim"]
        items: List[Dict[str, Any]] = []
        for row_number, row in enumerate(ws.iter_rows(min_row=2), start=2):
            sku = str(val(row, i_sku) or "").strip()
            cn = str(val(row, i_cn) or "").strip()
            if not sku and not cn:
                continue
            minimum_raw = val(row, i_price)
            minimum = resolve_formula(minimum_raw, ws)
            if minimum is None and isinstance(minimum_raw, str) and minimum_raw.strip().startswith("="):
                minimum = to_number(cached_value(row_number, i_price))
                detail = "已使用 Excel 缓存值" if minimum is not None else "且无可用缓存值"
                print(f"[警告] 最低售价第 {row_number} 行公式无法求值，{detail}", file=sys.stderr)
            dimensions = val(row, i_dim)
            if isinstance(dimensions, str) and dimensions.startswith("="):
                dimensions = cached_value(row_number, i_dim)
            shipping_mode = val(row, cols["shipping_mode"])
            items.append({
                "idx": len(items) + 1,
                "sku": sku,
                "cn": cn,
                "minimum_price": minimum,
                "img": str(val(row, i_img) or "").strip(),
                "prefix": str(val(row, cols["prefix"]) or "").strip().upper(),
                "stock_sku": str(val(row, cols["stock_sku"]) or "").strip(),
                "shipping_mode": str(shipping_mode).strip() if shipping_mode is not None else "",
                "dimensions": dimensions,
            })
    finally:
        wb.close()
        if cached is not None:
            cached.close()
    names = Counter(row['cn'] for row in items if row['cn'])
    seller_skus = {row['sku'] for row in items if row['sku']}
    for row in items:
        row['seller_sku'] = row['sku']
        row['name_unique'] = names[row['cn']] == 1
        if not row['sku'] and row['name_unique'] and row['cn'] not in seller_skus:
            row['sku'] = row['cn']
    return items

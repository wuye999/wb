# -*- coding: utf-8 -*-
"""Excel 表头解析工具（映射表 / 单店表 / 他人表 / 商品价格表共用）。

背景（2026-10-20）：价格列从「双倍售价」改名「实际售价」，且**只认新名**——
读取一律按表头名定位，不再依赖固定列位（`row[3]` / `cell(r,4)` / `idx.get(名, 2)`）。
缺列时的策略由调用方决定：
- 可重建的表（映射总表 / 单店表）→ `warn_missing()` 告警降级，避免阻断 `wb.py merge` 自愈；
- 自家必需的表（商品价格表）与外部不可重建的表（他人映射表）→ `require_col()` 抛 HeaderError。
"""
import re
import sys

# ---- 映射表 / 单店表 / 他人表共用的列名协议（写端与读端唯一来源）----
COL_CN = "产品中文名"
COL_VC = "vendorCode"
COL_ACTUAL_PRICE = "实际售价"
COL_TITLE = "俄文标题"
COL_IMG = "主图链接"
COL_L = "尺寸长(cm)"
COL_W = "尺寸宽(cm)"
COL_H = "尺寸高(cm)"
COL_WT = "毛重(kg)"
COL_NM = "WB商品码"
COL_SHOPS = "店铺覆盖"
COL_SHOP_CNY_RE = r"^店铺\d+价格\(CNY\)$"

# 旧列名：**仅用于报错/提示文案**，不做读取兼容（用户口径：只认新名）
LEGACY_ACTUAL_PRICE = "双倍售价"

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
    """必需列定位失败 → HeaderError（消息含表名/路径/缺列/实际表头/旧名提示）。"""
    idx = find_col(headers, name)
    if idx is not None:
        return idx
    where = f"{table}（{path}）" if path else table
    hint = ""
    if name == COL_ACTUAL_PRICE and LEGACY_ACTUAL_PRICE in headers:
        hint = (f"；检测到旧列名「{LEGACY_ACTUAL_PRICE}」，已废弃——"
                f"请把该列改名为「{COL_ACTUAL_PRICE}」（映射表可运行 wb.py merge 重建）")
    raise HeaderError(
        f"[表头错误] {where} 缺少必需列「{name}」{hint}；实际表头：{_headers_repr(headers)}")


def warn_missing(table, path, headers, name, hint=""):
    """可降级读取点：缺列时告警一次（同 表+文件+列 去重），由调用方把值置空。"""
    key = (table, str(path), name)
    if key in _WARNED:
        return
    _WARNED.add(key)
    tail = f"；{hint}" if hint else ""
    msg = (f"[警告] {table}（{path}）缺少列「{name}」{tail}；"
           f"实际表头：{_headers_repr(headers)}")
    if name == COL_ACTUAL_PRICE and LEGACY_ACTUAL_PRICE in headers:
        msg += f"（旧列名「{LEGACY_ACTUAL_PRICE}」已废弃，请改名或重建）"
    print(msg, file=sys.stderr)


def reset_warnings():
    """清空告警去重缓存（测试隔离用）。"""
    _WARNED.clear()

"""publish report。"""

import csv


import os

import unicodedata
from datetime import datetime
from typing import Any, List, Tuple

from sellerops.domain.models import PublishItem


from sellerops.settings import paths as config


def create_publish_csv(prefix_name: str = "上架") -> Tuple[Any, Any, str]:
    """初始化上架 CSV 执行记录文件"""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    csv_path = os.path.join(config.LOG_DIR, f"{prefix_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(csv_file)
    writer.writerow(["时间", "WB商品码", "vendorCode", "中文名", "价格", "包装长", "包装宽", "包装高", "毛重", "目标店", "库存", "结果", "原因"])
    return csv_file, writer, csv_path

def _display_width(s: Any) -> int:
    """计算字符串终端显示列宽（全角/CJK 占 2 列，半角占 1 列）"""
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in str(s))

def _pad_str(s: Any, width: int, align: str = "<") -> str:
    """对齐字符串以满足指定显示宽度"""
    s = str(s)
    dw = _display_width(s)
    pad = max(0, width - dw)
    return (" " * pad + s) if align == ">" else (s + " " * pad)

def print_publish_preview(items: List[PublishItem], target_shops_default: List[int]):
    """打印格式化预览表格（动态适配中英文字符宽度与原始VC溯源）"""
    if not items:
        print("\n[预览] 无待上架商品")
        return

    has_orig = any(it.extra and (it.extra.get("orig_vc") or it.extra.get("orig_nm")) for it in items)

    col_nm = 14
    col_vc = max(32, max((len(it.vendor_code or "") for it in items), default=30) + 2)
    col_orig = 32 if has_orig else 0
    col_cn = 16
    col_price = 10
    col_dims = 22
    col_stock = 6
    col_shops = 16

    header = (
        _pad_str("WB现商品码", col_nm) +
        _pad_str("上架新VC", col_vc) +
        (_pad_str("原始VC", col_orig) if has_orig else "") +
        _pad_str("中文名", col_cn) +
        _pad_str("售价(元)", col_price) +
        _pad_str("包装规格(长*宽*高/重)", col_dims) +
        _pad_str("库存", col_stock) +
        _pad_str("目标店铺", col_shops)
    )

    sep_len = _display_width(header)
    print("\n" + "=" * sep_len)
    print(header)
    print("-" * sep_len)

    for it in items:
        sids = it.target_shops or target_shops_default
        shops_str = ",".join(map(str, sids))
        dims_str = f"{it.length}*{it.width}*{it.height}/{it.weight}kg"
        p_val = float(it.price or 0.0)
        p_str = str(int(p_val)) if p_val.is_integer() else f"{p_val:.2f}"
        orig_vc = (it.extra.get("orig_vc") or it.extra.get("orig_nm")) if it.extra else "-"

        row = (
            _pad_str(it.nm_id, col_nm) +
            _pad_str(it.vendor_code or "-", col_vc) +
            (_pad_str(orig_vc or "-", col_orig) if has_orig else "") +
            _pad_str(it.cn or "-", col_cn) +
            _pad_str(p_str, col_price) +
            _pad_str(dims_str, col_dims) +
            _pad_str(it.stock, col_stock) +
            _pad_str(shops_str, col_shops)
        )
        print(row)

    print("=" * sep_len)

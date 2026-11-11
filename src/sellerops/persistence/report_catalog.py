"""报告只读取商品名称和VC归属，不依赖价格、图片或库存字段。"""
from pathlib import Path

from openpyxl import load_workbook

from sellerops.domain.product_reports import ReportCatalog
from sellerops.persistence.headers import COL_CN, COL_VC, read_headers, require_col
from sellerops.settings import paths


def _names(path: str | Path, sheet: str, mapping: bool) -> list[tuple[str, str]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        headers = read_headers(ws)
        cn_col = require_col(headers, COL_CN, sheet, str(path))
        vc_col = require_col(headers, COL_VC, sheet, str(path)) if mapping else None
        result = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            cn = str(row[cn_col] or '').strip()
            vc = str(row[vc_col] or '').strip() if mapping else ''
            if cn and (vc or not mapping):
                result.append((vc, cn))
        return result
    finally:
        wb.close()


def load_mapped_names() -> dict[str, str]:
    """按完整 VC 读取映射总表，拒绝冲突的商品归属。"""
    mapped_names = {}
    for vc, cn in _names(paths.MAPPING_XLSX, '映射总表', True):
        if vc in mapped_names and mapped_names[vc] != cn:
            raise ValueError('映射表同一完整VC对应多个中文名')
        mapped_names[vc] = cn
    return mapped_names


def load_report_catalog() -> ReportCatalog:
    """按表头读取本轮名称；损坏/缺失明确报错，不把旧报告归属冒充最新映射。"""
    return ReportCatalog(price_names={cn for _, cn in _names(paths.PRICE_BOOK_XLSX, 'Sheet1', False)},
                         mapped_names=load_mapped_names())

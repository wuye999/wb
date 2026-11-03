"""改码专用只读输入与提交历史；不使用会吞错或按前缀兜底的映射加载器。"""
from pathlib import Path
from typing import Any

from sellerops.core.files import safe_load_json
from sellerops.domain.vc_normalization import NormalizationCatalog, RenameAttempt
from sellerops.persistence.headers import COL_CN, COL_VC, read_headers, require_col
from sellerops.persistence.price_book import BOSS_COLUMNS
from sellerops.settings import paths


COMMAND = 'normalize-vc-openapi'
ATTEMPT_STATUSES = {'submitting', 'accepted', 'unknown'}


def _column(ws: Any, header: str, path: Path) -> int:
    headers = read_headers(ws)
    index = require_col(headers, header, ws.title, str(path))
    if headers.count(header) != 1:
        raise ValueError(f'{path.name} 的「{header}」表头重复')
    return index


def _text(value: Any) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('中文名、VC 与前缀必须为文本')
    return value.strip()


def _read_ownership(path: str) -> dict[str, set[str]]:
    data = safe_load_json(path, default={})
    if not isinstance(data, dict):
        raise ValueError(f'{Path(path).name} 必须为归属对象')
    result: dict[str, set[str]] = {}
    for vc, info in data.items():
        if not _text(vc) or not isinstance(info, dict):
            raise ValueError(f'{Path(path).name} 含无效归属记录')
        cn = _text(info.get('cn'))
        if cn:
            result.setdefault(vc.strip().upper(), set()).add(cn)
    return result


def load_catalog() -> NormalizationCatalog:
    """按表头读取所需两列；不依赖价格、图片、包装、库存或快照。"""
    import openpyxl

    result = NormalizationCatalog(owners=_read_ownership(paths.VC_KNOWN_JSON),
                                  overrides=_read_ownership(paths.VC_OVERRIDE_JSON))
    excluded = safe_load_json(paths.VC_EXCLUDED_JSON, default={})
    if not isinstance(excluded, dict):
        raise ValueError('vc_excluded.json 必须为排除对象')
    result.excluded.update(_text(vc).upper() for vc in excluded)
    path = Path(paths.PRICE_BOOK_XLSX)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb['Sheet1']
        cn_col, prefix_col = _column(ws, COL_CN, path), _column(ws, BOSS_COLUMNS['prefix'], path)
        for row in ws.iter_rows(min_row=2, values_only=True):
            cn, prefix = _text(row[cn_col]), _text(row[prefix_col]).upper()
            if cn:
                result.prefixes.setdefault(cn, []).append(prefix)
    finally:
        wb.close()

    path = Path(paths.MAPPING_XLSX)
    if path.exists():
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb['映射总表']
            cn_col, vc_col = _column(ws, COL_CN, path), _column(ws, COL_VC, path)
            for row in ws.iter_rows(min_row=2, values_only=True):
                vc, cn = _text(row[vc_col]), _text(row[cn_col])
                if vc and cn:
                    result.owners.setdefault(vc.upper(), set()).add(cn)
            if '已排除清单' in wb.sheetnames:
                ws = wb['已排除清单']
                vc_col = _column(ws, COL_VC, path)
                for row in ws.iter_rows(min_row=2, values_only=True):
                    vc = _text(row[vc_col])
                    if vc:
                        result.excluded.add(vc.upper())
        finally:
            wb.close()
    return result


def load_attempts() -> list[RenameAttempt]:
    """损坏日志阻止提交；预览及从未发送的项不占用历史分配。"""
    result = []
    for path in sorted(Path(paths.LOG_DIR).glob(f'{COMMAND}_*.json')):
        doc = safe_load_json(path)
        if (not isinstance(doc, dict) or doc.get('version') != 1 or doc.get('command') != COMMAND
                or type(doc.get('apply')) is not bool or not isinstance(doc.get('items'), list)):
            raise ValueError(f'历史改码日志格式无效：{path.name}')
        for row in doc['items']:
            if (not isinstance(row, dict) or row.get('status') not in
                    (ATTEMPT_STATUSES | {'planned', 'skipped', 'blocked', 'failed'})):
                raise ValueError(f'历史改码日志项目无效：{path.name}')
            if not doc['apply'] or row['status'] not in ATTEMPT_STATUSES:
                continue
            sid, nm = row.get('shopId'), row.get('nmID')
            old, new = row.get('vc'), row.get('newVC')
            if (type(sid) is not int or sid <= 0 or type(nm) is not int or nm <= 0
                    or not isinstance(old, str) or not old.strip() or old != old.strip()
                    or not isinstance(new, str) or not new.strip() or new != new.strip()):
                raise ValueError(f'历史改码日志缺少提交标识：{path.name}')
            result.append(RenameAttempt(sid, nm, old, new))
    return result

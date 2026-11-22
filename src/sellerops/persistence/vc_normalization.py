"""改码的明确归属、价格表前缀及提交历史。"""
import json
import sqlite3
from pathlib import Path

from sellerops.domain.vc_normalization import NormalizationCatalog, RenameAttempt
from sellerops.persistence import db
from sellerops.persistence.headers import read_headers, require_col
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.price_book import BOSS_COLUMNS
from sellerops.settings import paths


COMMAND = 'normalize-vc-openapi'
ATTEMPT_STATUSES = {'submitting', 'accepted', 'unknown'}


def load_catalog() -> NormalizationCatalog:
    """改码只取已确认归属，继续核对主表前缀表头。"""
    import openpyxl

    path = Path(paths.current.price_book_xlsx)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb['Sheet1']
        headers = read_headers(ws)
        require_col(headers, BOSS_COLUMNS['prefix'], ws.title, str(path))
        if headers.count(BOSS_COLUMNS['prefix']) != 1:
            raise ValueError('价格表前缀表头重复')
    finally:
        wb.close()
    products = MappingRepository.load_price_book(require_price=False, fresh=True)
    result = NormalizationCatalog()
    for row in products:
        result.prefixes.setdefault(row['cn'], []).append(row['prefix'])
    entries = db.vc_entries()
    for vc in entries:
        row = db.resolve_vc(vc, entries)
        if row['status'] == 'mapped':
            result.owners.setdefault(vc.upper(), set()).add(row['cn'])
        elif row['status'] == 'excluded':
            result.excluded.add(vc.upper())
    return result


def attempts_in(doc: dict, name: str) -> list[RenameAttempt]:
    if (not isinstance(doc, dict) or doc.get('version') != 1 or doc.get('command') != COMMAND
            or type(doc.get('apply')) is not bool or not isinstance(doc.get('items'), list)):
        raise ValueError(f'历史改码记录格式无效：{name}')
    result = []
    for row in doc['items']:
        if (not isinstance(row, dict) or row.get('status') not in
                (ATTEMPT_STATUSES | {'planned', 'skipped', 'blocked', 'failed'})):
            raise ValueError(f'历史改码记录项目无效：{name}')
        if not doc['apply'] or row['status'] not in ATTEMPT_STATUSES:
            continue
        sid, nm = row.get('shopId'), row.get('nmID')
        old, new = row.get('vc'), row.get('newVC')
        if (type(sid) is not int or sid <= 0 or type(nm) is not int or nm <= 0
                or not isinstance(old, str) or not old.strip() or old != old.strip()
                or not isinstance(new, str) or not new.strip() or new != new.strip()):
            raise ValueError(f'历史改码记录缺少提交标识：{name}')
        result.append(RenameAttempt(sid, nm, old, new))
    return result


def write_log(connection: sqlite3.Connection, name: str, document: dict, *, historical: bool = False) -> None:
    attempts_in(document, name)
    statuses = {row['status'] for row in document['items']}
    if 'submitting' in statuses:
        status = 'submitting'
    elif 'unknown' in statuses:
        status = 'unknown'
    elif statuses & {'failed', 'blocked'}:
        status = 'partial'
    else:
        status = 'done' if 'accepted' in statuses else 'draft'
    timestamp = document.get('createdAt') if historical else None
    db.save_plan(connection, 'normalize:' + name, 'normalize_vc', status, name, document, timestamp)


def load_attempts() -> list[RenameAttempt]:
    return [attempt for row in db.plans('normalize_vc', 'normalize:')
            for attempt in attempts_in(json.loads(row['payload_json']), row['dedup_key'])]

"""一次性离线迁移旧归属、快照与提交记录。"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from sqlite3 import Connection

from sellerops.core.files import safe_load_json
from sellerops.domain.snapshot_values import positive_id
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.persistence import db, open_api_publish, vc_normalization
from sellerops.persistence.headers import COL_CN, COL_VC, read_headers, require_col
from sellerops.persistence.mappings import save_products
from sellerops.persistence.price_book import read_price_book
from sellerops.persistence.snapshots import save_shop, save_snapshot
from sellerops.settings import paths


def _json(path: str | Path, default, expected: type):
    try:
        document = safe_load_json(path) if Path(path).exists() else default
    except (OSError, ValueError) as error:
        raise ValueError(f'无法读取旧文件 {Path(path).name}') from error
    if not isinstance(document, expected):
        raise ValueError(f'旧文件 {Path(path).name} 的格式无效')
    return document


def _sheet_owners(sheet) -> dict[str, list[tuple[str, str]]]:
    headers = read_headers(sheet)
    vc_index = require_col(headers, COL_VC, sheet.title)
    name_index = require_col(headers, COL_CN, sheet.title)
    type_index = headers.index('匹配类型') if '匹配类型' in headers else None
    owners = defaultdict(list)
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if row[vc_index] and row[name_index]:
            source = str(row[type_index] or '') if type_index is not None else ''
            owners[str(row[vc_index])].append((str(row[name_index]).strip(), source))
    return owners


def _read_workbooks() -> tuple[dict, dict, dict]:
    from openpyxl import load_workbook

    data = paths.current
    owners = defaultdict(list)
    excluded = {}
    output_shops = {}
    total = Path(data.mapping_xlsx)
    if total.is_file():
        workbook = load_workbook(total, read_only=True, data_only=True)
        try:
            if '映射总表' in workbook:
                owners.update(_sheet_owners(workbook['映射总表']))
            if '已排除清单' in workbook:
                sheet = workbook['已排除清单']
                headers = read_headers(sheet)
                key = require_col(headers, COL_VC, sheet.title)
                reason = headers.index('排除原因') if '排除原因' in headers else None
                for row in sheet.iter_rows(min_row=2, values_only=True):
                    if row[key]:
                        excluded[str(row[key])] = str(row[reason] or '') if reason is not None else ''
        finally:
            workbook.close()
    for status, folder in (('archived', data.shops_archive_dir), ('active', data.shops_dir)):
        for file in sorted(Path(folder).glob('shop_*.xlsx')):
            match = re.fullmatch(r'shop_(\d+)(?:_(.+))?', file.stem)
            if not match:
                continue
            output_shops[int(match[1])] = (match[2] or '', status)
            workbook = load_workbook(file, read_only=True, data_only=True)
            try:
                if '在架映射明细' in workbook:
                    for vc, candidates in _sheet_owners(workbook['在架映射明细']).items():
                        owners[vc].extend(candidates)
            finally:
                workbook.close()
    return owners, excluded, output_shops


def _listing_restrictions() -> dict[str, tuple[str, str]]:
    root = Path(paths.current.data_dir)
    restrictions = {}
    for file in (root / 'exclude_vcs.json', root / '上架排除清单/exclude_vcs.json'):
        if not file.is_file():
            continue
        source = _json(file, [], (list, dict))
        entries = source.get('exclusion_union') if isinstance(source, dict) else source
        if not isinstance(entries, list):
            raise ValueError('上架排除清单缺少 exclusion_union')
        for entry in entries:
            info = entry if isinstance(entry, dict) else {'vc': entry}
            vc = info.get('vc')
            if not isinstance(vc, str) or not vc.strip():
                raise ValueError('上架排除清单缺少完整 VC')
            shops = info.get('shop_ids')
            if shops is None:
                scope = 'ALL'
            elif isinstance(shops, list) and shops and all(positive_id(s) for s in shops):
                scope = ','.join(str(s) for s in sorted({positive_id(s) for s in shops}))
            else:
                raise ValueError('上架排除清单含无效店铺范围')
            old_scope, old_note = restrictions.get(vc, ('', ''))
            if 'ALL' in (old_scope, scope):
                scope = 'ALL'
            elif old_scope:
                scope = ','.join(sorted(set(old_scope.split(',') + scope.split(','))))
            reasons = info.get('reasons') or info.get('reason')
            note = '、'.join(map(str, reasons)) if isinstance(reasons, list) else str(reasons or '')
            restrictions[vc] = (scope, '；'.join(dict.fromkeys(r for r in (old_note, note) if r)))
    return restrictions


def _write_owners(connection: Connection, products: list[dict], sheet: dict, excluded: dict) -> dict:
    data = paths.current
    known = _json(data.vc_known_json, {}, dict)
    overrides = _json(data.vc_override_json, {}, dict)
    excluded.update(_json(data.vc_excluded_json, {}, dict))
    restrictions = _listing_restrictions()
    if any(not isinstance(row, dict) for row in (*known.values(), *overrides.values())):
        raise ValueError('旧归属池或纠偏记录格式无效')
    by_name = defaultdict(list)
    by_sku = {}
    for row in products:
        if row['cn']:
            by_name[row['cn']].append(row)
            if row['sku']:
                by_sku[row['sku']] = row
    fixed, pending = [], []
    filled_sku = 0
    for vc in sorted(set(known) | set(overrides) | set(excluded) | set(sheet) | set(restrictions)):
        base, override = known.get(vc, {}), overrides.get(vc, {})
        block_listing, block_note = restrictions.get(vc, (None, ''))
        flags = {'block_listing': block_listing, 'updated_at': override.get('updatedAt')}
        if vc in excluded:
            reason = str(excluded[vc] or '人工排除')
            db.set_vc(connection, vc, 'excluded', source='manual', note=reason, **flags)
            continue
        prior_sku = str(base.get('sku') or '').strip()
        base_name = str(base.get('cn') or '').strip()
        override_name = str(override.get('cn') or '').strip()
        report_names = {name for name, _ in sheet.get(vc, [])}
        names = ({override_name} if override_name else report_names | ({base_name} if base_name else set()))
        product = None
        note = str(override.get('reason') or '')
        if len(names) == 1:
            cn = next(iter(names))
            candidates = by_name[cn]
            if len(candidates) == 1 and candidates[0]['sku']:
                product = candidates[0]
            elif (not override_name and prior_sku in by_sku
                  and (not candidates or by_sku[prior_sku]['cn'] == cn)):
                product = by_sku[prior_sku]
        elif not names and prior_sku in by_sku:
            product = by_sku[prior_sku]
        if product is None:
            reason = '旧归属中文名冲突' if len(names) > 1 else '找不到唯一商品归属' if names else '原本未归属'
            pending.append({'vc': vc, 'oldSku': prior_sku, 'names': sorted(names), 'reason': reason})
            db.set_vc(connection, vc, 'pending', note='；'.join(filter(None, (reason, block_note))), **flags)
            continue
        sku = product['sku']
        if prior_sku and prior_sku != sku:
            fixed.append({'vc': vc, 'oldSku': prior_sku, 'sku': sku, 'cn': product['cn']})
        elif not prior_sku:
            filled_sku += 1
        base_source = base.get('source') or ''
        sheet_sources = {source for _, source in sheet.get(vc, [])}
        if override_name:
            source = 'manual'
        elif base_source in {'prefix', '前缀自动'} or '前缀自动' in sheet_sources:
            source = 'prefix'
        elif not base and product['prefix'] and product['prefix'] == extract_vc_prefix(vc):
            source = 'prefix'
        else:
            source = base_source if base_source in {'ai', 'publish', 'manual'} else 'manual'
        db.set_vc(connection, vc, 'mapped', sku=sku, source=source,
                  note='；'.join(filter(None, (note, block_note))), **flags)
    return {'correctedSku': fixed, 'filledMissingSku': filled_sku, 'ownershipIssues': pending}


def _write_snapshots(connection: Connection, shop_files: dict) -> list[dict]:
    data = paths.current
    status = _json(data.status_json, {}, dict)
    stores = {}
    for row in status.get('shops', []):
        sid = positive_id(row.get('id'))
        if sid is None:
            raise ValueError('旧同步状态中有无效店铺 ID')
        name, state = shop_files.get(sid, ('', 'active'))
        stores[sid] = (row.get('name') or name, state)
    for sid, value in shop_files.items():
        stores.setdefault(sid, value)
    for sid, (name, state) in stores.items():
        save_shop(connection, sid, name, state)
    result = []
    for file in sorted(Path(data.products_dir).glob('shop*_products_all.json')):
        match = re.fullmatch(r'shop(\d+)_products_all', file.stem)
        if match is None:
            continue
        sid = int(match[1])
        document = _json(file, None, (dict, list))
        try:
            count = save_snapshot(connection, sid, document)
        except ValueError as error:
            raise ValueError(f'{file.name}：{error}') from error
        result.append({'shopId': sid, 'rows': count})
    for row in status.get('errors', []):
        sid = positive_id(row.get('id'))
        if sid is None:
            raise ValueError('旧同步状态中有无效失败店铺 ID')
        save_shop(connection, sid, row.get('name') or '')
        recorded_at = status.get('fetchedAt')
        connection.execute('''
            UPDATE shops SET last_sync_error = ? WHERE shop_id = ?
             AND (last_synced_at IS NULL OR ? IS NULL OR last_synced_at <= ?)
        ''', (str(row.get('error') or ''), str(sid), recorded_at, recorded_at))
    return result


def _write_plans(connection: Connection) -> dict:
    data = paths.current
    records = _json(data.records_json, {}, dict)
    count = open_api_publish.write_shared(connection, records)
    open_state = _json(Path(data.state_dir) / 'WB_OpenAPI上架记录.json', {'version': 1, 'items': {}}, dict)
    open_api_publish.write_state(connection, open_state)
    histories = sorted(Path(data.log_dir).glob(f'{vc_normalization.COMMAND}_*.json'))
    for path in histories:
        document = _json(path, None, dict)
        vc_normalization.write_log(connection, path.stem, document, historical=True)
    return {'legacyPublishKeys': count, 'openApiItems': len(open_state['items']),
            'normalizeLogs': len(histories)}


def migrate() -> dict:
    data = paths.current
    if (Path(data.state_dir) / '.mapping-pending.json').exists():
        raise ValueError('旧映射保存尚未完成，请先在备份副本上恢复旧操作再迁移')
    with db.connect(write=True, migration=True) as connection:
        for table in db.TABLES:
            if connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                raise ValueError('sellerops.db 已有数据，旧文件没有覆盖数据库')
        book = Path(data.price_book_xlsx)
        stat = book.stat()
        products = read_price_book(str(book))
        current = book.stat()
        if (current.st_mtime_ns, current.st_size) != (stat.st_mtime_ns, stat.st_size):
            raise ValueError('商品价格表在迁移期间有改动，请关闭并重试')
        sheet, excluded, shops = _read_workbooks()
        save_products(connection, products, stat)
        result = _write_owners(connection, products, sheet, excluded)
        result['snapshots'] = _write_snapshots(connection, shops)
        result['history'] = _write_plans(connection)
        result['vcStatus'] = dict(Counter(row[0] for row in connection.execute('SELECT status FROM vc_map')))
        result['prefixMapped'] = connection.execute(
            "SELECT count(*) FROM vc_map WHERE status = 'mapped' AND source = 'prefix'").fetchone()[0]
        result['listingRestrictions'] = connection.execute(
            "SELECT count(*) FROM vc_map WHERE block_listing IS NOT NULL").fetchone()[0]
        result['productsWithoutSku'] = sum(not row['seller_sku'] for row in products)
        return result


def run(_args) -> int:
    result = migrate()
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0

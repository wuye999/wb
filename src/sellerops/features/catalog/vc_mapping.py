"""商品认领、推荐及六列 Excel 审核。"""
import json
import sys
from collections import defaultdict
from pathlib import Path

from sellerops.domain.snapshot_values import first_price, positive_id
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository
from sellerops.settings import paths


HEADERS = ('完整 VC', '出现店铺', '缩略图或链接', '平台标题与规格', 'AI 建议中文名', '运营最终确认')


def pending_groups(limit: int | None = None) -> list[dict]:
    products = MappingRepository.load_price_book(require_price=False)
    by_prefix = defaultdict(list)
    for product in products:
        if product['prefix']:
            by_prefix[product['prefix']].append(product)
    with db.connect() as connection:
        entries = db.vc_entries(connection)
        rows = connection.execute("""
            SELECT i.vc, i.row_json, s.shop_id, s.shop_name
              FROM shop_items i JOIN shops s USING(shop_id)
             WHERE s.status='active' AND i.is_trashed=0 AND i.vc IS NOT NULL
             ORDER BY i.vc, CAST(s.shop_id AS INTEGER)
        """).fetchall()
    pending: dict[str, dict] = {}
    for row in rows:
        vc = row['vc']
        if db.resolve_vc(vc, entries)['status'] != 'pending':
            continue
        if vc not in pending:
            raw = json.loads(row['row_json'])
            prefix = extract_vc_prefix(vc)
            matches = by_prefix.get(prefix, [])
            price = first_price(raw)
            ranked = sorted((p for p in products if p['dp'] is not None and price is not None),
                            key=lambda p: (abs(p['dp'] - price), p['sku']))[:3]
            sizes = [size for size in raw.get('sizeList') or [] if isinstance(size, dict)]
            pending[vc] = {'vc': vc, 'source_code': (entries.get(vc) or {}).get('source_code') or db.source_code(vc),
                           'title': raw.get('title') or '', 'image_url': raw.get('repImg') or '',
                           'price': price, 'size_count': len(sizes),
                           'sizes': [size.get('techSize') or size.get('name') or size.get('chrtId') for size in sizes],
                           'shops': [], 'prefix_candidate': candidate(matches[0]) if len(matches) == 1 else None,
                           'price_candidates': [candidate(product) for product in ranked]}
        pending[vc]['shops'].append({'shop_id': row['shop_id'], 'shop_name': row['shop_name']})
    groups: dict[tuple, list[dict]] = {}
    for item in pending.values():
        if item['source_code']:
            key = ('source', item['source_code'])
        elif item['title'] and item['image_url']:
            key = ('content', item['title'], item['image_url'])
        else:
            key = ('vc', item['vc'])
        groups.setdefault(key, []).append(item)
    order = sorted(groups.values(), key=lambda items: (not any(i['prefix_candidate'] for i in items), items[0]['vc']))
    return [{'source_code': items[0]['source_code'], 'vcs': [item['vc'] for item in items], 'items': items}
            for items in order[:limit]]


def candidate(product: dict) -> dict:
    return {'sku': product['sku'], 'cn': product['cn'], 'price': product['dp']}


def update_mappings(rows: list[dict]) -> list[str]:
    if not isinstance(rows, list):
        raise ValueError('确认内容应为包含完整 VC 的列表')
    MappingRepository.load_price_book(require_price=False, fresh=True)
    with db.connect(write=True) as connection:
        entries = db.vc_entries(connection)
        products = list(connection.execute('SELECT sku, cn, prefix FROM products'))
        shops = {row[0] for row in connection.execute('SELECT shop_id FROM shops')}
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('vc'), str):
                raise ValueError('确认记录缺少完整 VC')
            vc = row['vc'].strip()
            if vc not in entries or vc in seen:
                raise ValueError(f'完整 VC 不存在或重复：{vc}')
            seen.add(vc)
            old = entries[vc]
            note = str(row.get('note') or '')
            action = row.get('action')
            if action in ('map', 'rebind'):
                value = str(row.get('sku') or row.get('cn') or '').strip()
                sku_matches = [p for p in products if p['sku'] == value]
                matches = sku_matches or [p for p in products if p['cn'] == value]
                if len(matches) != 1:
                    raise ValueError(f'{vc} 找不到唯一卖家 SKU，请按 SKU 确认')
                source = row.get('source', 'manual')
                if source not in ('manual', 'ai', 'prefix'):
                    raise ValueError(f'{vc} 确认来源无效')
                prefix = extract_vc_prefix(vc)
                if source == 'prefix' and (not prefix or matches[0]['prefix'] != prefix
                                           or sum(p['prefix'] == prefix for p in products) != 1):
                    raise ValueError(f'{vc} 前缀与商品不唯一匹配')
                db.set_vc(connection, vc, 'mapped', sku=matches[0]['sku'], source=source, note=note)
            elif action == 'exclude':
                db.set_vc(connection, vc, 'excluded', source='manual', note=note or '人工确认非货盘')
            elif action in ('block', 'unblock'):
                scope = row.get('shops', 'ALL')
                if action == 'unblock':
                    block = ''
                elif scope == 'ALL':
                    block = scope
                elif (isinstance(scope, list) and scope and all(positive_id(s) and str(s) in shops for s in scope)):
                    block = ','.join(sorted({str(s) for s in scope}, key=int))
                else:
                    raise ValueError(f'{vc} 禁上架范围应是 ALL 或有效店铺 ID 列表')
                db.set_vc(connection, vc, old['status'], sku=old['sku'], source=old['source'],
                          block_listing=block, note=note or old['note'] or '')
            else:
                raise ValueError(f'{vc} 确认动作无效')
    return [row['vc'].strip() for row in rows]


def run_candidates(args) -> int:
    if args.limit < 1:
        raise ValueError('候选组数应为正整数')
    groups = pending_groups(args.limit)
    args.output = {'groups': groups}
    args.summary = f'列出 {len(groups)} 组待认领商品'
    args.next_action = '展示候选并请运营明确确认完整 VC'
    print(args.summary)
    for group in groups:
        for item in group['items']:
            print(f"{item['vc']} | {item['title']} | {item['price']}")
    return 0


def run_batch_set(args) -> int:
    text = Path(args.file).read_text(encoding='utf-8-sig') if args.file else sys.stdin.read()
    document = json.loads(text)
    items = document.get('items') if isinstance(document, dict) else document
    return show_updates(args, update_mappings(items))


def show_updates(args, vcs: list[str]) -> int:
    args.output = {'updated_vcs': vcs}
    args.summary = f'已保存 {len(vcs)} 条商品确认'
    print(args.summary)
    return 0


def run_export(args) -> int:
    from openpyxl import Workbook
    from sellerops.features.catalog.mapping_sync import _export_destination

    file = _export_destination('待确认商品.xlsx', args.out)
    book = Workbook()
    sheet = book.active
    sheet.title = '待确认商品'
    sheet.append(HEADERS)
    for group in pending_groups():
        for item in group['items']:
            suggestion = item['prefix_candidate'] or next(iter(item['price_candidates']), {})
            shops = '、'.join(s['shop_name'] for s in item['shops'])
            summary = f"{item['title']}（{item['size_count']} 个规格）"
            sheet.append((item['vc'], shops, item['image_url'], summary, suggestion.get('cn', ''), None))
            for cell in sheet[sheet.max_row][:5]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
    sheet.freeze_panes = 'B2'
    sheet.auto_filter.ref = sheet.dimensions
    for letter, width in zip('ABCDEF', (28, 24, 36, 58, 24, 24)):
        sheet.column_dimensions[letter].width = width
    try:
        book.save(file)
    finally:
        book.close()
    args.output = {'file': str(file), 'rows': sheet.max_row - 1}
    args.summary = '已导出待确认商品'
    print(file)
    return 0


def run_import(args) -> int:
    from openpyxl import load_workbook

    book = load_workbook(args.file, read_only=True, data_only=False)
    rows = []
    try:
        sheet = book['待确认商品']
        header = next(sheet.values, ())
        if tuple(header) != HEADERS:
            raise ValueError('请使用 map export-pending 导出的六列表')
        for index, row in enumerate(sheet.iter_rows(min_row=2), start=2):
            vc, final = row[0], row[5]
            if final.value is None or not str(final.value).strip():
                continue
            if final.data_type == 'f' or vc.data_type == 'f' or not vc.value:
                raise ValueError(f'第 {index} 行的完整 VC 或最终确认无效')
            value = str(final.value).strip()
            rows.append({'vc': str(vc.value), 'action': 'exclude' if value == '排除' else 'map',
                         'sku': value})
    finally:
        book.close()
    return show_updates(args, update_mappings(rows) if rows else [])

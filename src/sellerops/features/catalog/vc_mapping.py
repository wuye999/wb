"""待认领商品分类和完整 VC 批量确认。"""
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from sellerops.domain.snapshot_values import first_price, positive_id
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository


def multiplier_candidates(price: int | float | None, products: list[dict]) -> list[dict]:
    """返回最接近现价的前五个最低售价倍数参考。"""
    if price is None:
        return []
    current = Decimal(str(price))
    if not current.is_finite() or current <= 0:
        return []
    hits = []
    for product in products:
        if product['minimum_price'] is None:
            continue
        minimum = Decimal(str(product['minimum_price']))
        if not minimum.is_finite() or minimum <= 0:
            continue
        multiplier = round(current / minimum)
        if multiplier < 1:
            continue
        expected = minimum * multiplier
        diff = abs(current - expected)
        rel = diff / expected
        if diff <= 1 or rel <= Decimal('0.02'):
            hits.append((rel, diff, multiplier, product, expected))
    hits.sort(key=lambda hit: (*hit[:3], hit[3]['sku']))
    return [
        {**candidate(product), 'multiplier': multiplier,
         'expected_price': float(expected), 'diff': float(diff)}
        for _, diff, multiplier, product, expected in hits[:5]
    ]


def pending_groups(limit: int | None = None, *, products: list[dict] | None = None) -> list[dict]:
    if products is None:
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
        raw = json.loads(row['row_json'])
        image = raw.get('repImg')
        image = image.strip() if isinstance(image, str) else ''
        nm_id = positive_id(raw.get('nmId'))
        price = first_price(raw)
        if vc not in pending:
            prefix = extract_vc_prefix(vc)
            matches = by_prefix.get(prefix, [])
            sizes = [size for size in raw.get('sizeList') or [] if isinstance(size, dict)]
            pending[vc] = {'vc': vc, 'source_code': (entries.get(vc) or {}).get('source_code') or db.source_code(vc),
                           'title': raw.get('title') or '', 'image_url': image, 'nm_id': nm_id,
                           'price': price, 'size_count': len(sizes),
                           'sizes': [size.get('techSize') or size.get('name') or size.get('chrtId') for size in sizes],
                           'shops': [], 'prefix_candidate': candidate(matches[0]) if len(matches) == 1 else None}
        item = pending[vc]
        item['image_url'] = item['image_url'] or image
        item['nm_id'] = item['nm_id'] or nm_id
        if item['price'] is None:
            item['price'] = price
        item['shops'].append({'shop_id': row['shop_id'], 'shop_name': row['shop_name']})

    parents = {vc: vc for vc in pending}
    by_identity = {}
    by_price = {}

    def root(vc: str) -> str:
        while parents[vc] != vc:
            parents[vc] = parents[parents[vc]]
            vc = parents[vc]
        return vc

    for vc, item in pending.items():
        price = item['price']
        if price not in by_price:
            by_price[price] = multiplier_candidates(price, products)
        prices = by_price[price]
        item['price_candidates'] = prices
        item['category'] = 'prefix' if item['prefix_candidate'] else 'multiplier' if prices else 'unmatched'
        for field in ('source_code', 'image_url'):
            if value := item[field]:
                other = by_identity.setdefault((field, value), vc)
                parents[root(vc)] = root(other)

    by_group = defaultdict(list)
    for item in pending.values():
        by_group[root(item['vc'])].append(item)
    groups = []
    for items in by_group.values():
        prefixes = {p['sku']: p for item in items if (p := item['prefix_candidate'])}
        prefix = next(iter(prefixes.values())) if len(prefixes) == 1 else None
        category = ('prefix' if prefix else
                    'multiplier' if any(item['price_candidates'] for item in items) else 'unmatched')
        groups.append({'source_code': items[0]['source_code'], 'vcs': [item['vc'] for item in items],
                       'items': items, 'category': category, 'prefix_candidate': prefix})
    order = {'prefix': 0, 'multiplier': 1, 'unmatched': 2}
    return sorted(groups, key=lambda group: (order[group['category']], group['vcs'][0]))[:limit]


def candidate(product: dict) -> dict:
    return {'sku': product['sku'], 'cn': product['cn'], 'minimum_price': product['minimum_price']}


def update_mappings(rows: list[dict]) -> list[str]:
    if not isinstance(rows, list):
        raise ValueError('确认内容应为包含完整 VC 的列表')
    MappingRepository.load_price_book(require_price=False, fresh=True)
    with db.connect(write=True) as connection:
        entries = db.vc_entries(connection)
        products = list(connection.execute('SELECT sku, cn, prefix, name_unique FROM products'))
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
            if action in ('map', 'mapped', 'rebind'):
                sku = str(row.get('sku') or '').strip()
                name = str(row.get('cn') or '').strip()
                matches = [p for p in products if p['sku'] == sku and (not name or p['cn'] == name)]
                if not matches and not (sku and name):
                    matches = [p for p in products if p['cn'] == (name or sku) and p['name_unique']]
                if len(matches) != 1:
                    raise ValueError(f'{vc} 找不到唯一商品，请按卖家 SKU 或唯一的产品中文名确认')
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
    args.summary = f'列出 {len(groups)} 组待认领商品' if groups else '当前没有待认领商品'
    if groups:
        args.next_action = '展示候选并请运营明确确认完整 VC'
    print(args.summary)
    for group in groups:
        for item in group['items']:
            print(f"{item['vc']} | {item['title']} | {item['price']}")
    return 0


def run_batch_set(args) -> int:
    text = Path(args.file).read_text(encoding='utf-8-sig') if args.file else sys.stdin.read()
    vcs = update_mappings(json.loads(text))
    args.output = {'updated_vcs': vcs}
    args.summary = f'已保存 {len(vcs)} 条商品确认'
    print(args.summary)
    return 0

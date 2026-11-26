"""待认领商品分类和完整 VC 批量确认。"""
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from sellerops.domain.snapshot_values import first_price, positive_id
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository, read_current_price_book, save_price_book


_ASSIGN_ACTIONS = ('map', 'mapped', 'rebind')
_UPDATE_LABELS = {'mapped': '归属', 'rebound': '换绑', 'excluded': '排除', 'blocked': '禁上架',
                  'unblocked': '解除限制', 'unchanged': '业务字段相同'}
_VC_RECEIPT_LIMIT = 100


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


def _prepare_updates(rows: list[dict], entries: dict[str, dict], products: list[dict],
                     shops: set[str], counts: dict[str, int]) -> dict[str, dict]:
    updates = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('vc'), str):
            raise ValueError('确认记录缺少完整 VC')
        vc = row['vc'].strip()
        if not vc or vc not in entries or vc in updates:
            raise ValueError(f'完整 VC 不存在或重复：{vc}')
        old = entries[vc]
        note = str(row.get('note') or '')
        action = row.get('action')
        block = old['block_listing']
        if action in _ASSIGN_ACTIONS:
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
            status, sku = 'mapped', matches[0]['sku']
            outcome = 'rebound' if old['status'] == 'mapped' and old['sku'] != sku else 'mapped'
        elif action == 'exclude':
            status, sku, source = 'excluded', None, 'manual'
            note = note or '人工确认非货盘'
            outcome = 'excluded'
        elif action in ('block', 'unblock'):
            scope = row.get('shops', 'ALL')
            if action == 'unblock':
                block = ''
            elif scope == 'ALL':
                block = scope
            elif isinstance(scope, list) and scope and all(positive_id(s) and str(s) in shops for s in scope):
                block = ','.join(sorted({str(s) for s in scope}, key=int))
            else:
                raise ValueError(f'{vc} 禁上架范围应是 ALL 或有效店铺 ID 列表')
            status, sku, source = old['status'], old['sku'], old['source']
            note = note or old['note'] or ''
            outcome = 'blocked' if action == 'block' else 'unblocked'
        else:
            raise ValueError(f'{vc} 确认动作无效')
        note.encode('utf-8')  # SQLite TEXT requires UTF-8, including in the read-only preflight.
        update = dict(status=status, sku=sku, source=source, block_listing=block, note=note)
        updates[vc] = update
        if counts:
            unchanged = (all(old[key] == update[key] for key in ('status', 'sku', 'source'))
                         and (old['note'] or '') == note
                         and (old['block_listing'] or '') == (block or ''))
            counts['unchanged' if unchanged else outcome] += 1
    return updates


def _review_mappings(rows: list[dict], *, dry_run: bool = False,
                     summarize: bool = False) -> tuple[list[str], dict[str, int]]:
    if not isinstance(rows, list):
        raise ValueError('确认内容应为包含完整 VC 的列表')
    book = read_current_price_book() if any(isinstance(row, dict) and row.get('action') in _ASSIGN_ACTIONS
                                            for row in rows) else None
    counts = dict.fromkeys(_UPDATE_LABELS, 0) if summarize else {}
    with db.connect(write=not dry_run, read_only=dry_run) as connection:
        entries = db.vc_entries(connection)
        products = book.rows if book is not None else []
        shops = {row[0] for row in connection.execute('SELECT shop_id FROM shops')}
        updates = _prepare_updates(rows, entries, products, shops, counts)
        if summarize:
            active_vcs = connection.execute("""
                SELECT DISTINCT i.vc FROM shop_items i JOIN shops s USING (shop_id)
                 WHERE s.status = 'active' AND i.is_trashed = 0 AND i.vc IS NOT NULL
            """)
            counts['remaining_pending'] = sum(
                (updates.get(vc) or db.resolve_vc(vc, entries))['status'] == 'pending' for vc, in active_vcs
            )
        if not dry_run:
            if book is not None:
                save_price_book(connection, book)
            for vc, update in updates.items():
                db.set_vc(connection, vc, **update)
        if book is not None:
            book.verify()
    return list(updates), counts


def update_mappings(rows: list[dict]) -> list[str]:
    vcs, _ = _review_mappings(rows)
    return vcs


def run_candidates(args) -> int:
    if args.limit < 1:
        raise ValueError('候选组数应为正整数')
    all_groups = pending_groups()
    counts = dict.fromkeys(('prefix', 'multiplier', 'unmatched'), 0)
    counts.update(groups=len(all_groups), vcs=0)
    for group in all_groups:
        counts[group['category']] += 1
        counts['vcs'] += len(group['vcs'])
    groups = all_groups[:args.limit]
    truncated = len(groups) < len(all_groups)
    args.output = dict(groups=groups, counts=counts, limit=args.limit, returned_groups=len(groups),
                       returned_vcs=sum(len(group['vcs']) for group in groups), truncated=truncated)
    if groups:
        listed = f'本次列出前 {len(groups)} 组' if truncated else '本次全部列出'
        args.summary = f'共 {counts["groups"]} 组（{counts["vcs"]} 个 VC），{listed}'
        args.next_action = '展示候选并请运营明确确认完整 VC'
    else:
        args.summary = '当前没有待认领商品（共 0 组、0 个 VC，本次列出 0 组）'
    print(args.summary)
    for group in groups:
        for item in group['items']:
            print(f"{item['vc']} | {item['title']} | {item['price']}")
    return 0


def run_batch_set(args) -> int:
    text = Path(args.file).read_text(encoding='utf-8-sig') if args.file else sys.stdin.read()
    vcs, counts = _review_mappings(json.loads(text), dry_run=args.dry_run, summarize=True)
    shown = vcs if args.full else vcs[:_VC_RECEIPT_LIMIT]
    truncated = len(shown) < len(vcs)
    args.output = dict(dry_run=args.dry_run, counts=counts, updated_vcs=shown,
                       updated_vcs_total=len(vcs), updated_vcs_truncated=truncated)
    remaining = counts['remaining_pending']
    if args.dry_run:
        args.summary = f'只读预检通过，拟处理 {len(vcs)} 条商品确认'
        followup = ('保存后用 map workbench 继续核对剩余待认领' if remaining
                    else '保存后用 map check-mismatch 看图复核')
        args.next_action = (f'确认后用同一份审核数据去掉 --dry-run 重新运行。{followup}' if vcs
                            else '请补充审核记录后再预检')
        pending_label = '预计保存后剩余待认领'
    else:
        args.summary = f'已保存 {len(vcs)} 条商品确认'
        args.next_action = (f'用 map workbench 继续看图核对剩余 {remaining} 个待认领 VC' if remaining
                            else '用 map check-mismatch 看图复核归属')
        pending_label = '剩余待认领'
    print(args.summary)
    details = ' · '.join(f'{label} {counts[key]} 条' for key, label in _UPDATE_LABELS.items())
    print(f'{details} · {pending_label} {remaining} 个 VC')
    if truncated:
        print(f'JSON 默认列出前 {_VC_RECEIPT_LIMIT} 个 VC，可用 --full 查看全量')
    print(args.next_action)
    return 0

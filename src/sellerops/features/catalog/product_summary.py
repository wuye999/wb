"""只读汇总在营店快照中的已归属商品。"""
from collections import defaultdict
from dataclasses import dataclass
import json

from sellerops.domain.snapshot_values import positive_id, price_value, stock_at, valid_sizes
from sellerops.domain.warehouses import select_warehouse
from sellerops.persistence import db
from sellerops.settings import credentials


@dataclass(frozen=True)
class CardValues:
    currency: str | None
    prices: list[int | float | None]
    discount: int | float | None
    stock: int | None


def _snapshot_warehouse(shop: dict, document: dict, keyword: str) -> dict:
    reason = '快照没有采集时核实的默认业务仓'
    warehouse = document.get('warehouse')
    if (shop['last_source'] == document.get('source') == 'wb-openapi'
            and str(document.get('shopId')) == str(shop['shop_id']) and isinstance(warehouse, dict)):
        try:
            selected = select_warehouse([warehouse], keyword)
        except ValueError as error:
            reason = str(error)
        else:
            return {**selected, 'status': 'verified_at_capture', 'reason': None}
    return {'id': None, 'name': None, 'status': 'unknown', 'reason': reason}


def _card_values(item, warehouse_id: int | None) -> CardValues:
    raw = json.loads(item['row_json'])
    sizes = valid_sizes(raw) or []
    currency = raw.get('currencyIsoCode')
    currency = (currency.strip().upper() or None) if isinstance(currency, str) else None
    discount = price_value(raw.get('discount'))
    stock = None
    if (warehouse_id is not None and sizes and
            (item['warehouse_id'] is None or positive_id(item['warehouse_id']) == warehouse_id)):
        amounts = [stock_at(size, warehouse_id) for size in sizes]
        if None not in amounts:
            total = sum(amounts)
            if item['stock'] in (None, total):
                stock = total
    return CardValues(currency, [price_value(size.get('price')) for size in sizes],
                      discount if discount is not None and discount <= 100 else None, stock)


def _ranges(cards: list[CardValues]) -> dict:
    by_currency = defaultdict(list)
    for card in cards:
        by_currency[card.currency].extend(price for price in card.prices if price is not None)
    discounts = [card.discount for card in cards if card.discount is not None]
    return {'item_count': len(cards),
            'original_price_by_currency': [
                {'currency': currency, 'min': min(values, default=None), 'max': max(values, default=None),
                 'known_specs': len(values)}
                for currency, values in sorted(by_currency.items(), key=lambda pair: pair[0] or '')],
            'unknown_price_items': sum(not card.prices or None in card.prices for card in cards),
            'discount_pct': {'min': min(discounts, default=None), 'max': max(discounts, default=None),
                             'known_items': len(discounts), 'unknown_items': len(cards) - len(discounts)}}


def _stock(cards: list[CardValues]) -> dict:
    known = [card.stock for card in cards if card.stock is not None]
    total = sum(known)
    return {'known_items': len(known), 'unknown_items': len(cards) - len(known),
            'in_stock_items': sum(value > 0 for value in known), 'zero_stock_items': known.count(0),
            'known_quantity': total if known else None,
            'total_quantity': total if len(known) == len(cards) else None}


def _query(args) -> tuple[list[dict], list[dict]]:
    wanted = set(map(int, args.shops.split(','))) if args.shops else None
    shops, groups = {}, defaultdict(lambda: defaultdict(list))
    with db.connect(read_only=True) as connection:
        rows = [dict(shop) for shop in connection.execute('''
            SELECT shop_id, shop_name, last_source, last_synced_at, last_sync_error, snapshot_json
              FROM shops WHERE status = 'active'
        ''') if wanted is None or int(shop['shop_id']) in wanted]
        if wanted is not None and (missing := wanted - {int(shop['shop_id']) for shop in rows}):
            raise ValueError(f'本地没有这些在营店铺：{sorted(missing)}')
        keyword = (credentials.get().wb_warehouse_keyword
                   if any(shop['last_source'] == 'wb-openapi' for shop in rows) else '')
        for shop in sorted(rows, key=lambda row: int(row['shop_id'])):
            document = shop.pop('snapshot_json')
            shop['snapshot_present'] = document is not None
            shop['unmapped_item_count'] = 0 if document is not None else None
            shop['warehouse'] = _snapshot_warehouse(shop, json.loads(document) if document else {}, keyword)
            shops[shop['shop_id']] = shop
            if document is None:
                continue
            for item in connection.execute('''
                SELECT i.row_json, i.warehouse_id, i.stock, p.cn
                  FROM shop_items AS i
                  LEFT JOIN vc_map AS m ON m.vc = i.vc
                  LEFT JOIN products AS p ON m.sku = p.sku AND m.status = 'mapped'
                 WHERE i.shop_id = ? AND i.is_trashed = 0 AND m.status IS NOT 'excluded'
            ''', (shop['shop_id'],)):
                name = item['cn']
                if not name:
                    shop['unmapped_item_count'] += 1
                elif ((not args.name or name == args.name)
                      and (not args.name_contains or args.name_contains in name)):
                    groups[name][shop['shop_id']].append(_card_values(item, shop['warehouse']['id']))
    return list(shops.values()), [
        {'name': name, 'shop_count': len(stores), **_ranges([card for cards in stores.values() for card in cards]),
         'per_shop': [{'shop_id': sid, **_ranges(cards), 'stock': _stock(cards)} for sid, cards in stores.items()]}
        for name, stores in groups.items()]


def _format_ranges(row: dict) -> str:
    prices = '、'.join(f"{r['min']}–{r['max']} {r['currency'] or '币种未知'}"
                      for r in row['original_price_by_currency'] if r['known_specs']) or '未知'
    percent = row['discount_pct']
    discount = f"{percent['min']}%–{percent['max']}%" if percent['known_items'] else '未知'
    return f'原价 {prices} | 折扣 {discount}'


def _print(shops: list[dict], groups: list[dict], matched: int) -> None:
    for shop in shops:
        warehouse = shop['warehouse']
        label = (f"{warehouse['name']}（{warehouse['id']}，采集时核实）" if warehouse['id'] else
                 f"未知（{warehouse['reason']}）")
        print(f"店{shop['shop_id']} {shop['shop_name']} | 来源 {shop['last_source'] or '未知'} | "
              f"同步 {shop['last_synced_at'] or '未同步'} | 默认业务仓 {label}")
    print(f'中文名汇总：显示 {len(groups)} / {matched} 组')
    for group in groups:
        print(f"{group['name']} | {group['item_count']} 条 / {group['shop_count']} 店 | {_format_ranges(group)}")
        for row in group['per_shop']:
            stock = row['stock']
            quantity = stock['total_quantity'] if stock['total_quantity'] is not None else '未知'
            if quantity == '未知' and stock['known_quantity'] is not None:
                quantity = f"未知（已知小计 {stock['known_quantity']}）"
            print(f"  店{row['shop_id']} {row['item_count']} 条 | {_format_ranges(row)} | 默认业务仓库存 {quantity} | "
                  f"有库 {stock['in_stock_items']} 条，零库存 {stock['zero_stock_items']} 条，"
                  f"未知 {stock['unknown_items']} 条")


def run(args) -> int:
    if args.limit < 0:
        raise ValueError('--limit 必须为非负整数（0 表示不限）')
    shops, groups = _query(args)
    keys = {'name': lambda row: row['name'], 'count': lambda row: (-row['item_count'], row['name']),
            'shops': lambda row: (-row['shop_count'], row['name'])}
    groups.sort(key=keys[args.sort])
    selected = groups[:args.limit] if args.limit else groups
    args.output = {'shops': shops, 'groups': selected, 'sort': args.sort, 'limit': args.limit,
                   'totals': {'selected_shops': len(shops), 'snapshot_shops': sum(s['snapshot_present'] for s in shops),
                              'unmapped_items': sum(s['unmapped_item_count'] or 0 for s in shops),
                              'matched_groups': len(groups), 'returned_groups': len(selected)}}
    _print(shops, selected, len(groups))
    return 0

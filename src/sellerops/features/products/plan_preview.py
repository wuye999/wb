"""小单兼容回执与大单预览摘要，使用冻结范围的只读投影。"""
from collections import Counter, defaultdict
from collections.abc import Iterator
import csv
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from sellerops.core.json_values import json_value
from sellerops.settings import paths


_ROW_FIELDS = ('shop_id', 'status', 'vc', 'cn', 'nmId', 'chrtId', 'warehouse_id', 'targets', 'reason')


def _row(sid: int | None, status: str, item: dict, targets: dict, *, wh=None, spec=None) -> dict:
    nm_id = item.get('nmID') or item.get('nm_id') or item.get('sourceNm')
    return {key: value for key, value in zip(_ROW_FIELDS, (
        sid, status, item.get('vc') or item.get('vendor_code'), item.get('cn'), nm_id,
        spec, wh, targets, item.get('reason') or item.get('msg'),
    )) if value is not None and value != {}}


def _product_rows(scope: list, action: str) -> Iterator[dict]:
    for shop in scope:
        sid = shop['shopId']
        for item in shop['items']:
            if action == 'trash':
                for spec, wh in item.get('stock_specs', []):
                    yield _row(sid, 'planned', item, {'amount': 0, 'move_to_trash': True}, wh=wh, spec=spec)
                continue
            targets = {field: item[field] for field in ('price', 'discount', 'clubDiscount', 'amount', 'keep_price')
                       if item.get(field) is not None}
            yield _row(sid, 'planned', item, targets, wh=item.get('warehouseId'), spec=item.get('chrtId'))
        for field, status in (('blocked_items', 'blocked'), ('skipped_items', 'skipped')):
            for item in shop.get(field, []):
                yield _row(sid, status, item, {}, spec=item.get('chrtId'))
        if shop.get('range_error') and not shop.get('blocked_items'):
            yield _row(sid, 'blocked', {'reason': shop['range_error']}, {})


def _package(item: dict) -> dict:
    return {key: item[key] for key in ('length', 'width', 'height', 'weight') if item.get(key) is not None}


def _publishing_rows(scope: dict, action: str) -> Iterator[dict]:
    warehouses = scope.get('warehouses', {})
    for item in scope.get('items', []):
        if action in {'create-cards', 'replicate'}:
            fields, _, payload = item
            for shop in json.loads(payload['shop']):
                yield _row(shop['id'], 'planned', fields,
                           {'price': fields['price'], 'stock': shop['warehouseQuantity'], 'package': _package(fields)},
                           wh=shop['warehouseId'])
        elif action == 'publish':
            for sid in item['targets']:
                yield _row(sid, 'planned', {**item, 'nmID': item['sku']},
                           {'price': item['item']['price'], 'stock': item['stock'], 'package': _package(item['item'])},
                           wh=warehouses.get(sid))
        else:
            yield _row(item.get('shopId'), 'planned', item, {'new_vc': item.get('newVC')})
    for key in ('create', 'continue'):
        for item in scope.get(key, []):
            variant = next(iter((item.get('card') or {}).get('variants', [])), {})
            sizes = variant.get('sizes', [])
            targets = {'operation': key, 'stock': item.get('stock'), 'price': sizes[0].get('price') if sizes else None}
            if variant.get('dimensions'):
                targets['package'] = variant['dimensions']
            yield _row(item.get('shopId'), 'planned', item, targets, wh=(item.get('warehouse') or {}).get('id'))
    for item in scope.get('blocked', []):
        status = 'skipped' if item.get('status') == 'skipped' else 'blocked'
        if item.get('targets'):
            for sid in item['targets']:
                yield _row(sid, status, item, {})
        else:
            yield _row(item.get('shopId'), status, item, {})


def _size(output: dict) -> int:
    return len(json.dumps(output, ensure_ascii=False, default=json_value).encode('utf8'))


def _write_plan_csv(action: str, rows: list[dict]) -> str:
    now = datetime.now()
    folder = paths.daily_log_dir(now)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f'商品操作预览_{now:%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.csv'
    with output.open('x', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(['操作', '店铺ID', '状态', '完整VC', '商品中文名', '商品ID', '规格ID', '仓库ID', '目标值', '原因'])
        for row in rows:
            values = [row.get(key) if key != 'targets' else json.dumps(row.get(key, {}), ensure_ascii=False)
                      for key in _ROW_FIELDS]
            writer.writerow([action, *values])
    print(f'[完整计划预览 CSV] {output.resolve()}')
    return str(output.resolve())


def preview_output(output: dict, *, compact: bool) -> None:
    scope, result, action = output['scope'], output['result'], output['action']
    artifacts = dict(result.get('artifacts', {}))
    if isinstance(scope, list):
        for plan in scope:
            if report := plan.get('skip_report'):
                artifacts.setdefault('skips_csv', report)
    output['artifacts'] = {key: str(Path(path).resolve()) for key, path in artifacts.items() if path}
    if not compact:
        return
    rows = list(_product_rows(scope, action) if isinstance(scope, list) else _publishing_rows(scope, action))
    receipts = result.get('receipts', [])
    if max(len(rows), len(receipts)) <= 20 and _size(output) <= 32 * 1024:
        return
    if rows and (action not in {'price', 'discount'} or not artifacts.get('receipts_csv')
                 or any(row.get('targets', {}).get('keep_price') for row in rows)):
        try:
            output['artifacts']['plan_csv'] = _write_plan_csv(action, rows)
        except OSError:
            output['preview_warning'] = '无法保存完整计划 CSV，已保留全量预览'
            print(f"[警告] {output['preview_warning']}")
            return
    per_shop = defaultdict(list)
    for row in rows:
        per_shop[row.get('shop_id')].append(row)
    counts = {status: sum(row['status'] == status for row in rows) for status in ('planned', 'blocked', 'skipped')}
    samples = [dict(row, **({'reason': row['reason'][:300]} if row.get('reason') else {})) for row in rows[:10]]
    output['preview'] = {'detail': 'summary', 'counts': {'total': len(rows), **counts},
                         'per_shop': [{'shop_id': sid, 'counts': dict(Counter(row['status'] for row in items)),
                                       'planned_cards': len({row.get('vc') for row in items if row['status'] == 'planned'}),
                                       'planned_specs': sum(row['status'] == 'planned' and row.get('chrtId') is not None
                                                            for row in items)} for sid, items in per_shop.items()],
                         'samples': samples, 'omitted': len(rows) - len(samples)}
    del output['scope']
    output['result'] = {key: value for key, value in result.items() if key != 'receipts'}

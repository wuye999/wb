"""Route the price-book shipping-group orders through WB FBS."""
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from openpyxl import load_workbook

from sellerops.core.errors import AuthenticationError, NetworkError, PlatformApiError, RateLimitError
from sellerops.core.files import FileLock, atomic_dump_json
from sellerops.core.utils import ensure_utf8_stdout
from sellerops.features.catalog.snapshot_open_api import resolve_moscow
from sellerops.features.listings.open_api_common import safe_reason, select_accesses
from sellerops.features.orders.fbs_pdfs import check_pair, export_pair, require_pdf_tools
from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.integrations.wildberries.fbs_api import BATCH_SIZE, FBSRequestError, WBFBSAPI
from sellerops.persistence.fbs_shipments import (
    SINGAPORE, journal_path, load_journal, pdf_paths, save_journal, supply_names,
)
from sellerops.persistence.headers import read_headers
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.price_book import BOSS_COLUMNS, GROUP_SHIPPING_MARKER, price_book_columns
from sellerops.persistence.report_catalog import load_mapped_names
from sellerops.settings import paths


_VALID_META = {
    'filled', 'optional', 'deadlineExceeded', 'imeiMaySell', 'imeiSoldB2B',
    'uinMaySell', 'sgtinIntroduced', 'sgtinSoldB2B',
}


@dataclass
class OrderGroup:
    product_name: str
    orders: list[dict]
    price_conflict: bool

    @property
    def ids(self) -> list[int]:
        return [row['id'] for row in self.orders]


def _catalog() -> tuple[dict[str, str], dict[str, set[str]]]:
    mapping = load_mapped_names()
    overrides = MappingRepository.load_vc_override()
    if not isinstance(overrides, dict):
        raise ValueError('VC 纠偏记录格式无效')
    for vc, override in overrides.items():
        if not isinstance(override, dict):
            raise ValueError('VC 纠偏记录格式无效')
        cn = str(override.get('cn') or '').strip()
        if vc in mapping and cn:
            mapping[vc] = cn
    sku_values = defaultdict(set)
    workbook = load_workbook(paths.PRICE_BOOK_XLSX, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        required = ('cn', 'stock_sku')
        columns = price_book_columns(sheet, paths.PRICE_BOOK_XLSX, required=required)
        headers = read_headers(sheet)
        if any(headers.count(BOSS_COLUMNS[key]) != 1 for key in required):
            raise ValueError('价格表商品中文名或库存 SKU 表头重复')
        for row in sheet.iter_rows(min_row=2, values_only=True):
            cn = str(row[columns['cn']] or '').strip()
            sku = str(row[columns['stock_sku']] or '').strip()
            if cn and sku:
                sku_values[cn].add(sku)
    finally:
        workbook.close()
    return mapping, dict(sku_values)


def _groups(orders: list[dict], warehouse_id: int, catalog: tuple,
            selected_name: str) -> tuple[list[OrderGroup], list[dict]]:
    mapping, sku_values = catalog
    groups, blocked = defaultdict(list), []
    for row in orders:
        warehouse = row.get('warehouseId')
        if type(warehouse) is int and warehouse > 0 and warehouse != warehouse_id:
            continue
        article = row.get('article')
        cn = mapping.get(article.strip()) if isinstance(article, str) else None
        if not cn:
            if warehouse == warehouse_id:
                blocked.append({'order_ids': [row['id']], 'vendor_code': article,
                                'reason': '莫斯科仓订单无法按完整 VC 确认中文名'})
            continue
        if GROUP_SHIPPING_MARKER not in sku_values.get(cn, ()) or (selected_name and cn != selected_name):
            continue
        if type(warehouse) is not int or warehouse != warehouse_id:
            blocked.append({'product_name': cn, 'order_ids': [row['id']],
                            'reason': '订单缺少可确认的卖家仓库 ID'})
        else:
            groups[cn].append(row)
    return [
        OrderGroup(cn, sorted(rows, key=lambda row: row['id']), len(sku_values[cn]) != 1)
        for cn, rows in sorted(groups.items())
    ], blocked


def _batches(ids: list[int]):
    for start in range(0, len(ids), BATCH_SIZE):
        yield ids[start:start + BATCH_SIZE]


def _check_group(api: WBFBSAPI, group: OrderGroup) -> bool:
    if group.price_conflict:
        raise ValueError('价格表同名商品同时有发货群标记和其他库存 SKU')
    profiles, metadata = set(), {}
    for row in group.orders:
        cargo, cross = row.get('cargoType'), row.get('crossBorderType')
        options = row.get('options')
        b2b = options.get('isB2B') if isinstance(options, dict) else None
        if (row.get('deliveryType') != 'fbs' or type(cargo) is not int or cargo < 1
                or type(cross) is not int or cross < 0 or type(b2b) is not bool):
            raise ValueError('订单缺少可确认的 FBS 货物尺寸、跨境或 B2B 属性')
        profiles.add((cargo, cross, b2b))
        required, optional = (row.get(field) for field in ('requiredMeta', 'optionalMeta'))
        required = [] if required is None else required
        optional = [] if optional is None else optional
        if any(not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
               for values in (required, optional)):
            raise ValueError('WB 订单标识要求格式无效')
        if required or optional:
            metadata[row['id']] = (set(required), set(optional))
    if len(profiles) != 1:
        raise ValueError('同组订单的货物尺寸、跨境或 B2B 属性不同，WB 无法合成一个供货')
    for ids in _batches(list(metadata)):
        for order_id, row in api.metadata(ids).items():
            required, optional = metadata[order_id]
            details = row.get('metaDetails')
            if not isinstance(details, list) or any(not isinstance(item, dict) for item in details):
                raise ValueError(f'订单 {order_id} 的标识状态响应无效')
            by_key = {item.get('key'): item for item in details if isinstance(item.get('key'), str)}
            if len(by_key) != len(details):
                raise ValueError(f'订单 {order_id} 的标识字段重复或无效')
            for key in sorted(required | optional):
                item = by_key.get(key, {})
                decision = item.get('decision')
                value = item.get('value')
                if (not isinstance(decision, str) or decision not in _VALID_META
                        or (key in required and (decision == 'optional'
                                                or not value or value == 'null'))):
                    raise ValueError(f'订单 {order_id} 的 {key} 标识缺失或未通过 WB 验证')
    return next(iter(profiles))[1] != 0


def _attempt(record: dict, save: Callable[[], None], step: str, status: int,
             call: Callable[[], str | None], *, ids: list[int] | None = None) -> None:
    receipt = {'step': step, 'result': 'submitting'}
    if ids is not None:
        receipt['order_ids'] = ids
    record['attempts'].append(receipt)
    record['stage'] = 'submitting'
    save()
    try:
        result = call()
    except FBSRequestError as exc:
        receipt.update(result='unknown' if exc.uncertain else 'rejected', reason=exc.public_reason,
                       http_status=exc.status_code)
        stage = 'rejected' if step == 'create' and not exc.uncertain else 'manual'
        record.update(stage=stage, reason=exc.public_reason)
        save()
        raise
    if step == 'create':
        record['supply_id'] = result
        print(f"  建供货受理 {result}｜{record['name']}", flush=True)
    receipt.update(result='accepted', http_status=status)
    record['stage'] = 'delivered' if step == 'deliver' else 'building'
    save()


def _submit(api: WBFBSAPI, record: dict, save: Callable[[], None]) -> None:
    _attempt(record, save, 'create', 201, lambda: api.create_supply(record['name']))
    supply_id = record['supply_id']
    for ids in _batches(record['order_ids']):
        _attempt(record, save, 'add', 204, lambda: api.add_orders(supply_id, ids), ids=ids)
    _attempt(record, save, 'deliver', 204, lambda: api.deliver(supply_id))
    print(f'  提交配送已受理 {supply_id}', flush=True)


def _export(api: WBFBSAPI, record: dict) -> None:
    qr_path, labels_path = pdf_paths(record)
    record['files'] = {'supply_qr': str(qr_path), 'order_labels': str(labels_path)}
    count = len(record['order_ids'])
    if qr_path.is_file() and labels_path.is_file():
        check_pair(qr_path, labels_path, count)
    else:
        qr = api.supply_qr(record['supply_id'])
        cross_border = record['cross_border']
        labels = {}
        for ids in _batches(record['order_ids']):
            labels.update(api.stickers(ids, cross_border=cross_border))
        export_pair(qr_path, labels_path, qr, [labels[order_id] for order_id in record['order_ids']],
                    cross_border=cross_border)
    record.update(stage='complete', label_pages=count)
    record.pop('reason', None)
    print(f'  已导出 {qr_path}\n  已导出 {labels_path}', flush=True)


def _shop_failed(exc: Exception) -> bool:
    return (isinstance(exc, (AuthenticationError, RateLimitError))
            or getattr(exc, 'status_code', None) in (401, 403, 429))


def _process_shop(shop: OpenAPIShop, args, catalog: tuple | None, supplies: list[dict],
                  names: set[str], save: Callable[[], None], results: list[dict]) -> None:
    api = WBFBSAPI(shop.api_key)
    try:
        warehouse = resolve_moscow(api.warehouses())
        print(f"{shop.name}({shop.id})｜{warehouse['name']} ID {warehouse['id']}", flush=True)
        if args.resume:
            pending = [r for r in supplies if r['shop_id'] == shop.id
                       and r['stage'] not in ('complete', 'rejected')
                       and (not args.name or r['product_name'] == args.name)]
            for record in pending:
                results.append(record)
                print(f"  {record['name']}｜{record.get('supply_id', '无供货号')}｜{record['stage']}")
                if record['stage'] != 'delivered' or not args.apply:
                    continue
                if record.get('warehouse_id') != warehouse['id']:
                    record['reason'] = '原供货仓库与当前本店莫斯科仓 ID 不一致'
                    save()
                    continue
                if _try_export(api, record, save):
                    results.extend(r for r in pending if r not in results)
                    return
            return

        groups, blocked = _groups(api.new_orders(), warehouse['id'], catalog, args.name)
        reserved = {order_id for r in supplies if r['shop_id'] == shop.id and r['stage'] != 'rejected'
                    for order_id in r['order_ids']}
        results.extend(dict(row, shop_id=shop.id, stage='blocked') for row in blocked)
        stop_reason = ''
        for group in groups:
            original, name = supply_names(datetime.now(SINGAPORE), shop.id, shop.name,
                                          group.product_name, len(group.ids), names)
            record = dict(shop_id=shop.id, shop_name=shop.name, warehouse_id=warehouse['id'],
                          product_name=group.product_name, order_ids=group.ids, name=name,
                          original_name=original, stage='preview', attempts=[])
            results.append(record)
            try:
                if stop_reason:
                    raise ValueError(stop_reason)
                if reserved.intersection(group.ids):
                    raise ValueError('含有既有供货日志中的订单，请按原供货记录处理')
                if any(row.get('product_name') == group.product_name for row in blocked):
                    raise ValueError('同组存在仓库无法确认的订单')
                record['cross_border'] = _check_group(api, group)
            except Exception as exc:
                record.update(stage='blocked', reason=safe_reason(exc))
                if _shop_failed(exc):
                    stop_reason = '本店令牌异常或限流'
                continue
            print(f'  {name}｜{len(group.ids)} 单', flush=True)
            if not args.apply:
                continue
            supplies.append(record)
            try:
                _submit(api, record, save)
            except FBSRequestError as exc:
                if _shop_failed(exc):
                    stop_reason = '本店令牌异常或限流'
                continue
            if _try_export(api, record, save):
                stop_reason = '本店令牌异常或限流'
    finally:
        api.close()


def _try_export(api: WBFBSAPI, record: dict, save: Callable[[], None]) -> bool:
    try:
        _export(api, record)
    except Exception as exc:
        record['reason'] = safe_reason(exc)
        save()
        return _shop_failed(exc)
    save()
    return False


def run(args) -> int:
    ensure_utf8_stdout()
    args.name = args.name.strip()
    if args.apply:
        require_pdf_tools()
    _, shops = select_accesses(args.shops)
    catalog = None if args.resume else _catalog()
    path = journal_path()
    results = []
    with FileLock(path) if args.apply else nullcontext():
        supplies = load_journal(path)
        names = {row['name'].casefold() for row in supplies}

        def save() -> None:
            save_journal(path, supplies)

        for shop in shops:
            try:
                _process_shop(shop, args, catalog, supplies, names, save, results)
            except (AuthenticationError, RateLimitError, PlatformApiError, NetworkError, ValueError) as exc:
                results.append(dict(shop_id=shop.id, shop_name=shop.name,
                                    stage='blocked', reason=safe_reason(exc)))
    failures = [row for row in results if row['stage'] not in ('complete', 'preview')
                and not (args.resume and not args.apply and row['stage'] == 'delivered'
                         and not row.get('reason'))]
    for row in failures:
        if row.get('reason'):
            print(f"  [{row['stage']}] {row.get('name') or row.get('product_name') or row['shop_id']}｜{row['reason']}")
    stamp = datetime.now(SINGAPORE).strftime('%Y%m%d_%H%M%S')
    report = Path(paths.LOG_DIR) / f'WB_FBS发货群_{stamp}_{uuid4().hex[:8]}.json'
    atomic_dump_json(report, {'apply': args.apply, 'resume': args.resume, 'results': results},
                     use_lock=False)
    print(f"完成 {sum(r['stage'] == 'complete' for r in results)} 组，"
          f"预览 {sum(r['stage'] == 'preview' for r in results)} 组，"
          f"待处理或失败 {len(failures)} 项｜明细 {report}")
    return int(bool(failures))

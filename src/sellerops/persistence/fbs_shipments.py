"""FBS supply journal and naming for the portal and exported files."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import re

from sellerops.core.errors import DataConsistencyError
from sellerops.core.files import atomic_dump_json, safe_load_json
from sellerops.settings import paths


SINGAPORE = timezone(timedelta(hours=8))
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _display(value: str, limit: int) -> str:
    clean = _UNSAFE_NAME.sub('-', value).strip(' .')
    if clean and clean == value and len(clean) <= limit:
        return clean
    digest = sha256(value.encode('utf8')).hexdigest()[:8]
    prefix = clean[:limit - 9].rstrip(' .') or '未命名'
    return f'{prefix}-{digest}'


def supply_names(now: datetime, shop_id: int, shop: str, product: str,
                 count: int, used: set[str]) -> tuple[str, str]:
    timestamp = now.astimezone(SINGAPORE).strftime('%Y%m%d-%H%M%S')
    original = f'{timestamp}_{shop}({shop_id})_{product}_{count}单'
    display = f'{timestamp}_{_display(shop, 22)}({shop_id})_{_display(product, 42)}_{count}单'
    name = display
    suffix = 2
    while name.casefold() in used:
        name = f'{display}_{suffix:02d}'
        suffix += 1
    used.add(name.casefold())
    return original, name


def pdf_paths(record: dict) -> tuple[Path, Path]:
    name, supply_id = record['name'], record['supply_id']
    if (_UNSAFE_NAME.search(name) or not re.match(r'^\d{8}-\d{6}_', name)
            or not re.fullmatch(r'WB-GI-[0-9]{1,20}', supply_id)):
        raise DataConsistencyError('供货日志中的文件名或供货号无效')
    directory = Path(paths.DATA_DIR) / 'fbs_shipments' / name[:8]
    shop = _display(record['shop_name'], 16)
    product = _display(record['product_name'], 24)
    count = len(record['order_ids'])
    number = supply_id.removeprefix('WB-GI-')
    prefix = f"{shop}({record['shop_id']})_{product}_{count}单_{number}"
    return directory / f'{prefix}_供货码.pdf', directory / f'{prefix}_标签.pdf'


def journal_path() -> Path:
    return Path(paths.STATE_DIR) / 'WB_FBS发货群供货.json'


def load_journal(path: Path) -> list[dict]:
    empty = {'version': 1, 'supplies': []}
    data = safe_load_json(path, default=empty)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('supplies'), list):
        raise DataConsistencyError(f'FBS 供货日志格式无效：{path}')
    seen_orders, seen_names = set(), set()
    for row in data['supplies']:
        if not isinstance(row, dict):
            raise DataConsistencyError(f'FBS 供货日志记录无效：{path}')
        ids = row.get('order_ids')
        if (type(row.get('shop_id')) is not int or row['shop_id'] < 1
                or any(not isinstance(row.get(key), str) or not row[key]
                       for key in ('name', 'shop_name', 'product_name'))
                or row.get('stage') not in ('submitting', 'building', 'rejected', 'manual', 'delivered', 'complete')
                or not isinstance(row.get('attempts'), list)
                or not isinstance(ids, list) or not ids
                or any(type(order_id) is not int or order_id < 1 for order_id in ids)
                or len(ids) != len(set(ids))):
            raise DataConsistencyError(f'FBS 供货日志记录无效：{path}')
        order_keys = {(row['shop_id'], order_id) for order_id in ids} if row['stage'] != 'rejected' else set()
        name_key = row['name'].casefold()
        if seen_orders.intersection(order_keys) or name_key in seen_names:
            raise DataConsistencyError(f'FBS 供货日志包含重复订单或供货名：{path}')
        if row['stage'] in ('delivered', 'complete'):
            if type(row.get('cross_border')) is not bool or not isinstance(row.get('supply_id'), str):
                raise DataConsistencyError(f'FBS 供货日志缺少已配送供货资料：{path}')
            pdf_paths(row)
        seen_orders.update(order_keys)
        seen_names.add(name_key)
    return data['supplies']


def save_journal(path: Path, supplies: list[dict]) -> None:
    atomic_dump_json(path, {'version': 1, 'supplies': supplies}, use_lock=False)

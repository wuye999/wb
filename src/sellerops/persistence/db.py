"""SellerOps 的商品、店铺与提交记录。所有路径跟随本次命令的数据目录。"""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from sellerops.core.json_values import json_value
from sellerops.settings import paths


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY NOT NULL,
    cn TEXT NOT NULL,
    actual_price REAL,
    minimum_price REAL,
    image_url TEXT,
    dimensions TEXT,
    prefix TEXT,
    stock_sku TEXT,
    book_mtime_ns INTEGER NOT NULL,
    book_size INTEGER NOT NULL,
    refreshed_at TEXT NOT NULL,
    name_unique INTEGER
);
CREATE TABLE IF NOT EXISTS vc_map (
    vc TEXT PRIMARY KEY NOT NULL,
    source_code TEXT,
    sku TEXT,
    status TEXT NOT NULL CHECK(status IN ('mapped', 'excluded', 'pending')),
    source TEXT CHECK(source IN ('prefix', 'ai', 'manual', 'publish')),
    block_listing TEXT,
    note TEXT,
    updated_at TEXT NOT NULL,
    CHECK((status = 'mapped') = (sku IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS vc_map_sku ON vc_map(sku);
CREATE TABLE IF NOT EXISTS shops (
    shop_id TEXT PRIMARY KEY NOT NULL,
    shop_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    last_source TEXT,
    last_synced_at TEXT,
    last_sync_error TEXT,
    snapshot_json TEXT
);
CREATE TABLE IF NOT EXISTS shop_items (
    shop_id TEXT NOT NULL REFERENCES shops(shop_id),
    vc TEXT,
    nm_id INTEGER,
    warehouse_id INTEGER,
    price REAL,
    discount REAL,
    stock INTEGER,
    sizes_json TEXT,
    title TEXT,
    image_url TEXT,
    is_trashed INTEGER NOT NULL,
    synced_at TEXT,
    row_json TEXT NOT NULL,
    PRIMARY KEY (shop_id, vc)
);
CREATE INDEX IF NOT EXISTS shop_items_nm ON shop_items(nm_id);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'submitting', 'done', 'partial', 'unknown')),
    dedup_key TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plans_action_key ON plans(action_type, dedup_key);
"""

TABLES = ('products', 'vc_map', 'shops', 'shop_items', 'plans')


def now() -> str:
    return datetime.now().isoformat(' ', timespec='seconds')


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=json_value)


def legacy_exists() -> bool:
    data = paths.current
    return (any(Path(name).exists() for name in (data.mapping_xlsx, data.vc_known_json,
                data.vc_override_json, data.vc_excluded_json, data.records_json, data.status_json))
            or (Path(data.state_dir) / 'WB_OpenAPI上架记录.json').exists()
            or (Path(data.state_dir) / '.mapping-pending.json').exists()
            or any(Path(data.shops_dir).glob('shop_*.xlsx'))
            or any(Path(data.shops_archive_dir).glob('shop_*.xlsx'))
            or (Path(data.data_dir) / 'exclude_vcs.json').exists()
            or any(Path(data.products_dir).glob('shop*_products_all.json'))
            or any(Path(data.log_dir).glob('normalize-vc-openapi_*.json'))
            or (Path(data.data_dir) / '上架排除清单/exclude_vcs.json').exists())


@contextmanager
def connect(*, write: bool = False, migration: bool = False) -> Iterator[sqlite3.Connection]:
    path = Path(paths.current.database)
    exists = path.exists()
    if not exists:
        if not migration and legacy_exists():
            raise ValueError('检测到旧业务数据，请先运行 sellerops migrate-legacy')
        if not write:
            connection = sqlite3.connect(':memory:')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=15)
    else:
        connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA foreign_keys = ON')
        if write or not exists:
            connection.executescript(SCHEMA)
        connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        if not migration and exists and not any(
            connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in TABLES
        ) and legacy_exists():
            raise ValueError('\u68c0\u6d4b\u5230\u65e7\u4e1a\u52a1\u6570\u636e\uff0c\u8bf7\u5148\u8fd0\u884c sellerops migrate-legacy')
        with connection:
            yield connection
    finally:
        connection.close()


def source_code(vc: str) -> str | None:
    return max(re.findall(r'\d+', vc), key=len, default=None)


def set_vc(connection: sqlite3.Connection, vc: str, status: str, *, sku: str | None = None,
           source: str | None = None, block_listing: str | None = None,
           note: str = '', updated_at: str | None = None) -> None:
    if not isinstance(vc, str) or not vc or vc != vc.strip():
        raise ValueError('归属记录缺少完整 VC')
    connection.execute("""
        INSERT INTO vc_map VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vc) DO UPDATE SET sku=excluded.sku, status=excluded.status,
            source=excluded.source, block_listing=coalesce(excluded.block_listing, vc_map.block_listing),
            note=excluded.note, updated_at=excluded.updated_at
    """, (vc, source_code(vc), sku, status, source, block_listing, note, updated_at or now()))


_VC_QUERY = """
    SELECT m.*, p.cn, p.actual_price AS dp, p.minimum_price, p.image_url AS img,
           p.dimensions, p.prefix, p.stock_sku
      FROM vc_map m LEFT JOIN products p ON m.sku = p.sku
"""


def vc_entries(connection: sqlite3.Connection | None = None) -> dict[str, dict]:
    if connection is None:
        with connect() as local:
            return vc_entries(local)
    return {row['vc']: dict(row) for row in connection.execute(_VC_QUERY)}


def resolve_vc(vc: str, entries: dict[str, dict] | None = None) -> dict:
    """按完整 VC 返回归属；sku 列关联主表商品键（卖家 SKU 或唯一中文名）。"""
    if entries is None:
        with connect() as connection:
            row = connection.execute(_VC_QUERY + ' WHERE m.vc = ?', (vc,)).fetchone()
        entry = dict(row) if row is not None else {}
    else:
        entry = entries.get(vc, {})
    if not entry or (entry['status'] == 'mapped' and not entry.get('cn')):
        return {**entry, 'vc': vc, 'status': 'pending', 'sku': None}
    return entry


def listing_blocked(vc: str, shop_id: int | str, entries: dict[str, dict] | None = None) -> bool:
    entry = resolve_vc(vc, entries)
    restriction = entry.get('block_listing') or ''
    return (entry['status'] == 'excluded' or restriction == 'ALL'
            or str(shop_id) in restriction.split(','))


def save_plan(connection: sqlite3.Connection, plan_id: str, action: str, status: str,
              key: str, payload: dict, timestamp: str | None = None) -> None:
    stamp = timestamp or now()
    connection.execute("""
        INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_id) DO UPDATE SET status=excluded.status,
            payload_json=excluded.payload_json, updated_at=excluded.updated_at
        WHERE plans.status != excluded.status OR plans.payload_json != excluded.payload_json
    """, (plan_id, action, status, key, dumps(payload), stamp, stamp))


def plans(action: str, prefix: str, connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
    if connection is None:
        with connect() as local:
            return plans(action, prefix, local)
    return connection.execute(
        'SELECT * FROM plans WHERE action_type = ? AND plan_id GLOB ? ORDER BY rowid',
        (action, prefix + '*')).fetchall()

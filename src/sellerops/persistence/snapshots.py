"""按店保存完整商品行，并保留旧快照的行和封装契约。"""
import json
import sqlite3
from typing import Any

from sellerops.domain.bcs_products import deduplicate_bcs_products
from sellerops.domain.snapshot_values import first_price, positive_id, stock_at, valid_sizes
from sellerops.domain.warehouses import moscow_warehouse
from sellerops.persistence import db


class ShopRows(dict[str, dict]):
    """在架商品索引，另保留不能作为操作目标的原行。"""

    def __init__(self) -> None:
        super().__init__()
        self.other_cards: list[dict] = []


def validate_document(shop_id: int, document: Any) -> dict:
    if isinstance(document, list):
        document = {'rows': document}
    if not isinstance(document, dict) or not isinstance(document.get('rows'), list):
        raise ValueError('商品快照缺少 rows 数组')
    if document.get('shopId') is not None and str(document['shopId']) != str(shop_id):
        raise ValueError('商品快照的 shopId 与目标店铺不一致')
    for key in ('total', 'fetchedCount'):
        if document.get(key) is not None and document[key] != len(document['rows']):
            raise ValueError('商品快照数量不完整或元信息不一致')
    rows = deduplicate_bcs_products(document['rows'], shop_id)
    return {**document, 'shopId': shop_id, 'total': len(rows), 'fetchedCount': len(rows), 'rows': rows}


def save_shop(connection: sqlite3.Connection, shop_id: int, name: str = '',
              status: str = 'active') -> None:
    connection.execute("""
        INSERT INTO shops (shop_id, shop_name, status) VALUES (?, ?, ?)
        ON CONFLICT(shop_id) DO UPDATE SET shop_name = CASE WHEN ? = '' THEN shops.shop_name
            ELSE excluded.shop_name END
    """, (str(shop_id), name or f'shop{shop_id}', status, name))


def save_snapshot(connection: sqlite3.Connection, shop_id: int, document: dict,
                  *, name: str = '') -> int:
    document = validate_document(shop_id, document)
    rows = document.pop('rows')
    save_shop(connection, shop_id, name)
    source = document.get('source')
    timestamp = document.get('fetchedAt')
    warehouse = document.get('warehouse')
    warehouse_id = None
    if source == 'wb-openapi' and isinstance(warehouse, dict):
        try:
            warehouse_id = moscow_warehouse([warehouse])['id']
        except ValueError:
            pass
    items = []
    for row in rows:
        vc = row.get('vendorCode') or row.get('goodsCode')
        vc = vc.strip() if isinstance(vc, str) and vc.strip() else None
        nm = positive_id(row.get('nmId') or row.get('nmID'))
        sizes = valid_sizes(row)
        stocks = [stock_at(size, warehouse_id) for size in sizes] if sizes and warehouse_id else []
        stock = sum(stocks) if stocks and None not in stocks else None
        items.append((str(shop_id), vc, nm, warehouse_id, first_price(row), row.get('discount'),
                      stock, db.dumps(row.get('sizeList')), row.get('title'), row.get('repImg'),
                      int(bool(row.get('trashedAt'))), timestamp, db.dumps(row)))
    connection.execute('DELETE FROM shop_items WHERE shop_id = ?', (str(shop_id),))
    connection.executemany('INSERT INTO shop_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', items)
    connection.execute("""
        UPDATE shops SET last_source = ?, last_synced_at = ?, last_sync_error = NULL, snapshot_json = ?
         WHERE shop_id = ?
    """, (source, timestamp, db.dumps(document), str(shop_id)))
    connection.executemany("""
        INSERT OR IGNORE INTO vc_map (vc, source_code, status, updated_at)
        VALUES (?, ?, 'pending', ?)
    """, ((vc, db.source_code(vc), timestamp or db.now()) for _, vc, *_ in items if vc))
    return len(rows)


class ProductSnapshotRepository:
    @classmethod
    def exists(cls, shop_id: Any) -> bool:
        with db.connect() as connection:
            return connection.execute("""
                SELECT 1 FROM shops WHERE shop_id = ? AND snapshot_json IS NOT NULL AND status = 'active'
            """, (str(shop_id),)).fetchone() is not None

    @classmethod
    def load_snapshot_document(cls, shop_id: Any) -> dict | None:
        with db.connect() as connection:
            shop = connection.execute("""
                SELECT snapshot_json FROM shops WHERE shop_id = ? AND status = 'active'
            """, (str(shop_id),)).fetchone()
            if shop is None or shop['snapshot_json'] is None:
                return None
            rows = connection.execute(
                'SELECT row_json FROM shop_items WHERE shop_id = ? ORDER BY rowid',
                (str(shop_id),)).fetchall()
            document = {**json.loads(shop['snapshot_json']), 'rows': [json.loads(row[0]) for row in rows]}
            return validate_document(int(shop_id), document)

    @classmethod
    def load_shop_products(cls, shop_id: Any) -> list[dict]:
        return (cls.load_snapshot_document(shop_id) or {}).get('rows', [])

    @classmethod
    def save_shop_products(cls, shop_id: Any, products: list[dict], use_lock: bool = True) -> None:
        with db.connect(write=True) as connection:
            save_snapshot(connection, int(shop_id), {'rows': products})

    @classmethod
    def load_shop_rows(cls, shop_id: Any) -> ShopRows | None:
        document = cls.load_snapshot_document(shop_id)
        if document is None:
            return None
        rows = ShopRows()
        for item in document['rows']:
            vc = item.get('vendorCode') or item.get('goodsCode')
            if item.get('trashedAt') or not isinstance(vc, str) or not vc.strip():
                rows.other_cards.append(item)
            else:
                rows[vc.strip()] = item
        return rows

    @classmethod
    def find_source(cls, nm_id: int, shop_id: int | None = None) -> dict | None:
        # Without an explicit source, a card is usable only if exactly one local row matches.
        query = '''SELECT i.row_json FROM shop_items i JOIN shops s USING (shop_id)
                    WHERE i.nm_id = ? AND i.is_trashed = 0 AND s.status = 'active' '''
        parameters = (nm_id,)
        if shop_id is not None:
            query += 'AND i.shop_id = ?'
            parameters += (str(shop_id),)
        with db.connect() as connection:
            rows = connection.execute(query + ' LIMIT 2', parameters).fetchall()
        return json.loads(rows[0][0]) if len(rows) == 1 else None

    @classmethod
    def shop_ids_from_disk(cls) -> list[int]:
        with db.connect() as connection:
            return sorted(int(row[0]) for row in connection.execute("""
                SELECT shop_id FROM shops WHERE status = 'active' AND snapshot_json IS NOT NULL
            """))

"""SQLite fixtures for offline command tests."""
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from sellerops.features.system import repository_update
from sellerops.persistence import db
from sellerops.persistence.mappings import read_current_price_book, save_price_book
from sellerops.persistence.snapshots import ProductSnapshotRepository, save_snapshot
from sellerops.settings import paths


def mock_non_git_checkout(test):
    test.enterContext(patch.object(repository_update, '_main_head', return_value=None))


def write_book(rows, headers=('卖家SKU', '产品中文名', '最低售价', 'vendorCode前缀码', '马帮库存SKU', '发货方式')):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Sheet1'
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    path = Path(paths.current.price_book_xlsx)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    book.close()


def map_vcs(mapping, *, source='manual'):
    book = read_current_price_book()
    with db.connect(write=True) as connection:
        save_price_book(connection, book)
        for vc, sku in mapping.items():
            db.set_vc(connection, vc, 'mapped', sku=sku, source=source)


def snapshot(shop, rows, *, snapshot_status=None, **metadata):
    with db.connect(write=True) as connection:
        return save_snapshot(connection, shop, {'rows': rows, **metadata}, snapshot_status=snapshot_status)


def document(shop):
    return ProductSnapshotRepository.load_snapshot_document(shop)


def shop_summary():
    with db.connect() as connection:
        shops = list(connection.execute('SELECT * FROM shops ORDER BY CAST(shop_id AS INTEGER)'))
    return {
        'shops': [{'id': int(row['shop_id']), 'name': row['shop_name']} for row in shops],
        'errors': [{'id': int(row['shop_id']), 'error': row['last_sync_error']} for row in shops
                   if row['last_sync_error']],
        'fetched': [{'id': int(row['shop_id']), 'total': document(row['shop_id'])['total']}
                    for row in shops if row['snapshot_json'] and not row['last_sync_error']],
    }


def corrupt_snapshot(shop, *, row_json=None, snapshot_json=None):
    with closing(sqlite3.connect(paths.current.database)) as connection, connection:
        if snapshot_json is not None:
            connection.execute('UPDATE shops SET snapshot_json=? WHERE shop_id=?', (snapshot_json, shop))
        if row_json is not None:
            connection.execute('UPDATE shop_items SET row_json=? WHERE shop_id=?', (row_json, shop))

"""SQLite migration, review and mapping exports from the normal CLI."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from catalog_fixture import document, map_vcs, shop_summary, snapshot, write_book
from sellerops.cli.main import main
from sellerops.core.files import atomic_dump_json
from sellerops.features.catalog import products
from sellerops.persistence import db, migrate_legacy, open_api_publish
from sellerops.persistence.mappings import MappingRepository
from sellerops.settings import paths


LIVE = 'BCS-TEST-ozon-card-1058-2'
OTHER = 'BCS-OTHR-source/201-2'
BACKUP = 'BCS-OLDX-3'
UNKNOWN = 'BCS-TEST-4'
EXCLUDED = 'BCS-TEST-5'


def row(vc=LIVE, nm=101, chrt=201):
    return {'vendorCode': vc, 'nmId': nm, 'createAt': '2025-01-01',
            'sizeList': [{'chrtId': chrt, 'price': 10, 'stockList': [
                {'warehouseId': 43, 'amount': 0}, {'warehouseId': 999, 'amount': 5}]}]}


class TestCatalogWorkflow(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(paths.configure, paths.current.data_dir)
        self.folder = Path(temporary.name) / 'data'
        paths.configure(self.folder)
        write_book([('SKU-A', '原名', 10, 'TEST', 'STOCK-A'), ('SKU-B', '新名', 20, 'OTHR', 'STOCK-B'),
                    ('SKU-C', '重名', 12, 'CUPA', ''), ('SKU-D', '重名', 14, 'CUPB', '')])
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def cli(self, *args):
        self.stdout.seek(0)
        self.stdout.truncate()
        self.stderr.seek(0)
        self.stderr.truncate()
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return main(['--data-dir', str(self.folder), *args])

    def legacy(self):
        atomic_dump_json(paths.current.vc_known_json, {
            LIVE: {'cn': '原名', 'sku': 'SKU-A', 'source': '前缀自动'},
            BACKUP: {'cn': '新名', 'sku': 'SKU-A'},
            EXCLUDED: {'cn': '原名', 'sku': 'SKU-A'},
            'AMBIGUOUS': {'cn': '重名'},
        })
        atomic_dump_json(paths.current.vc_override_json, {BACKUP: {'cn': '新名', 'reason': '纠偏'}})
        book = Workbook()
        book.active.title = '映射总表'
        book.active.append(['产品中文名', 'vendorCode', '匹配类型'])
        book.active.append(['新名', OTHER, '前缀自动'])
        book.active.append(['原名', EXCLUDED, '前缀自动'])
        excluded = book.create_sheet('已排除清单')
        excluded.append(['vendorCode', '排除原因'])
        excluded.append([EXCLUDED, '非货盘'])
        book.save(paths.current.mapping_xlsx)
        book.close()
        atomic_dump_json(self.folder / '上架排除清单/exclude_vcs.json', {
            'exclusion_union': [{'vc': LIVE, 'reasons': ['原禁上架']}]})
        atomic_dump_json(paths.shop_json_path(1), {
            'source': 'wb-openapi', 'fetchedAt': '2026-01-01', 'shopId': 1,
            'warehouse': {'id': 43, 'warehouseId': 999, 'name': '莫斯科'},
            'rows': [row(), row(OTHER, 102, 202), row(UNKNOWN, 103, 203), row(EXCLUDED, 104, 204)]
        })
        atomic_dump_json(paths.shop_json_path(2), {'rows': [row(nm=202, chrt=302)]})
        atomic_dump_json(paths.current.records_json, {LIVE: {'2': '2026-02-03'}, '123': {'2': '2026-02-03'}})
        atomic_dump_json(Path(paths.current.state_dir) / 'WB_OpenAPI上架记录.json', {
            'version': 1, 'items': {open_api_publish.item_key(1, OTHER):
                {'shopId': 1, 'vc': OTHER, 'stages': {}, 'status': 'unknown'}}})
        atomic_dump_json(Path(paths.current.log_dir) / 'normalize-vc-openapi_fixture.json', {
            'version': 1, 'command': 'normalize-vc-openapi', 'apply': True,
            'items': [{'shopId': 2, 'nmID': 202, 'vc': LIVE, 'newVC': 'BCS-OTHR-1058',
                       'status': 'submitting'}]
        })

    def test_migrate_uses_unique_names_when_sku_is_blank(self):
        write_book([('', '无 SKU 商品', 30, 'TEST', '库存编码'),
                    ('SKU-B', '重名', 23, 'AAAA', ''), ('', '重名', 24, 'BBBB', ''),
                    ('另一商品名', '别的商品', 30, 'CCCC', ''), ('', '另一商品名', 40, 'DDDD', '')])
        atomic_dump_json(paths.current.vc_known_json, {
            LIVE: {'cn': '无 SKU 商品', 'source': '前缀自动'}, OTHER: {'cn': '重名'},
            UNKNOWN: {'cn': '另一商品名'},
        })
        atomic_dump_json(paths.shop_json_path(1), {'rows': [row(), row(OTHER, 102), row(UNKNOWN, 103)]})
        self.assertEqual(self.cli('migrate-legacy'), 0, self.stderr.getvalue())
        owner = db.resolve_vc(LIVE)
        self.assertEqual((owner['status'], owner['sku'], owner['source'], owner['stock_sku']),
                         ('mapped', '无 SKU 商品', 'prefix', '库存编码'))
        self.assertEqual(db.resolve_vc(OTHER)['status'], 'pending')
        self.assertEqual(db.resolve_vc(UNKNOWN)['status'], 'pending')
        review = self.folder.parent / 'review.json'
        atomic_dump_json(review, [{'vc': OTHER, 'action': 'map', 'cn': '重名'}])
        self.assertEqual(self.cli('map', 'batch-set', '--file', str(review), '--json'), 1)
        atomic_dump_json(review, [{'vc': OTHER, 'action': 'map', 'sku': 'SKU-B'}])
        self.assertEqual(self.cli('map', 'batch-set', '--file', str(review), '--json'), 0)
        atomic_dump_json(review, [{'vc': UNKNOWN, 'action': 'map', 'cn': '无 SKU 商品'}])
        self.assertEqual(self.cli('map', 'batch-set', '--file', str(review), '--json'), 0)
        self.assertEqual(db.resolve_vc(UNKNOWN)['sku'], '无 SKU 商品')

    def test_missing_sku_header_and_older_product_cache(self):
        book = Path(paths.current.price_book_xlsx)
        write_book([('唯一中文名', 30, 'TEST')], headers=('产品中文名', '最低售价', 'vendorCode前缀码'))
        stat = book.stat()
        with closing(sqlite3.connect(paths.current.database)) as connection, connection:
            connection.executescript(db.SCHEMA.replace(',\n    name_unique INTEGER,\n    shipping_mode TEXT', ''))
            connection.execute('INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               ('OLD', '旧缓存', 10, '', '', '', '', stat.st_mtime_ns, stat.st_size, db.now()))
        self.assertEqual(self.cli('doctor', '--json'), 0)
        self.assertEqual(json.loads(self.stdout.getvalue())['data']['price_book']['status'], 'ready')
        [product] = MappingRepository.load_price_book()
        self.assertEqual((product['sku'], product['cn'], product['name_unique']), ('唯一中文名', '唯一中文名', 1))

    def test_old_database_adds_shipping_mode_and_refreshes_unchanged_book(self):
        write_book([('SKU-A', '原名', 10, 'TEST', 'STOCK-A', '马帮发货')])
        stat = Path(paths.current.price_book_xlsx).stat()
        with closing(sqlite3.connect(paths.current.database)) as connection, connection:
            connection.executescript(db.SCHEMA.replace(',\n    shipping_mode TEXT', '').replace(
                '    minimum_price REAL,', '    actual_price REAL,\n    minimum_price REAL,'))
            connection.execute('INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               ('SKU-A', '原名', 99, 10, '', '', 'TEST', 'OLD-STOCK',
                                stat.st_mtime_ns, stat.st_size, db.now(), 1))
            db.set_vc(connection, LIVE, 'mapped', sku='SKU-A', source='manual')

        with db.connect() as connection:
            columns = [row['name'] for row in connection.execute('PRAGMA table_info(products)')]
        self.assertEqual(columns.count('shipping_mode'), 1)
        owner = db.resolve_vc(LIVE)
        self.assertEqual((owner['stock_sku'], owner['shipping_mode']), ('OLD-STOCK', None))
        self.assertEqual(owner['minimum_price'], 10)

        [product] = MappingRepository.load_price_book()
        self.assertEqual((product['stock_sku'], product['shipping_mode']), ('STOCK-A', '马帮发货'))
        owner = db.resolve_vc(LIVE)
        self.assertEqual((owner['sku'], owner['stock_sku'], owner['shipping_mode']),
                         ('SKU-A', 'STOCK-A', '马帮发货'))
        with db.connect() as connection:
            self.assertEqual(tuple(connection.execute('SELECT actual_price, minimum_price FROM products').fetchone()),
                             (None, 10))

    def test_migration_preserves_prefix_ownership_and_fixes_unambiguous_sku(self):
        self.legacy()
        files = {p: p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        self.assertEqual(self.cli('migrate-legacy'), 0, self.stderr.getvalue())
        result = json.loads(self.stdout.getvalue().splitlines()[-1])
        self.assertEqual([item['vc'] for item in result['correctedSku']], [BACKUP])
        self.assertEqual(result['prefixMapped'], 2)
        self.assertEqual(db.resolve_vc(BACKUP)['sku'], 'SKU-B')
        self.assertEqual(db.resolve_vc(LIVE)['stock_sku'], 'STOCK-A')
        self.assertTrue(db.listing_blocked(LIVE, 1))
        self.assertEqual(db.resolve_vc(EXCLUDED)['status'], 'excluded')
        self.assertEqual(db.resolve_vc(UNKNOWN)['status'], 'pending')
        self.assertEqual(db.resolve_vc('AMBIGUOUS')['status'], 'pending')
        self.assertEqual(document(1)['rows'][0], row())
        self.assertEqual(document(2)['rows'][0], row(nm=202, chrt=302))
        with db.connect() as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertCountEqual([r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")], db.TABLES)
            self.assertEqual(tuple(connection.execute(
                'SELECT warehouse_id, stock FROM shop_items WHERE shop_id=? AND vc=?',
                ('1', LIVE)).fetchone()), (43, 0))
            self.assertEqual({r[0] for r in connection.execute('SELECT status FROM plans')},
                             {'unknown', 'submitting', 'done'})
        database = Path(paths.current.database)
        baseline = database.read_bytes()
        self.assertEqual(self.cli('migrate-legacy'), 1)
        self.assertEqual(database.read_bytes(), baseline)
        for path, before in files.items():
            self.assertEqual(path.read_bytes(), before, str(path))

    def test_invalid_snapshot_aborts_migration_and_regular_commands_still_require_migration(self):
        self.legacy()
        atomic_dump_json(paths.shop_json_path(2), {'shopId': 1, 'rows': []})
        self.assertEqual(self.cli('migrate-legacy'), 1)
        self.assertIn('shop2_products_all.json', self.stderr.getvalue())
        with closing(sqlite3.connect(paths.current.database)) as connection:
            for table in db.TABLES:
                self.assertEqual(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.cli('report', 'mapping'), 1)
        self.assertIn('migrate-legacy', self.stderr.getvalue())

    def test_interrupted_legacy_review_bundle_stops_before_import(self):
        self.legacy()
        atomic_dump_json(Path(paths.current.state_dir) / '.mapping-pending.json', {'files': {}})
        self.assertEqual(self.cli('migrate-legacy'), 1)
        self.assertFalse(Path(paths.current.database).exists())

    def test_missing_explicit_snapshot_is_different_from_empty_or_archived(self):
        snapshot(1, [])
        with db.connect(write=True) as connection:
            from sellerops.persistence.snapshots import save_shop
            save_shop(connection, 2, '旧店', 'archived')
            save_shop(connection, 3, '未拉取')
        self.assertEqual(document(1)['rows'], [])
        for shop in ('2', '3'):
            self.assertEqual(self.cli('report', 'mapping', '--shop-id', shop), 1)
        self.assertEqual(self.cli('report', 'mapping', '--shop-id', '1'), 0, self.stderr.getvalue())

    def test_review_applies_all_or_none_and_exports_without_old_files(self):
        self.legacy()
        self.assertEqual(self.cli('migrate-legacy'), 0, self.stderr.getvalue())
        for path in (paths.current.mapping_xlsx, paths.current.vc_known_json, paths.current.vc_override_json,
                     paths.shop_json_path(1), paths.shop_json_path(2)):
            Path(path).unlink()
        review = self.folder / 'review.json'
        good = {'vc': LIVE, 'action': 'mapped', 'sku': 'SKU-B', 'cn': '新名'}
        atomic_dump_json(review, [good, {'vc': UNKNOWN, 'action': 'mapped', 'cn': '重名'}])
        self.assertEqual(self.cli('catalog', 'merge', str(review)), 1)
        self.assertEqual(db.resolve_vc(LIVE)['sku'], 'SKU-A')
        self.assertIn('找不到唯一商品', self.stderr.getvalue())
        atomic_dump_json(review, [good])
        self.assertEqual(self.cli('catalog', 'merge', str(review)), 0, self.stderr.getvalue())
        self.assertEqual(db.resolve_vc(LIVE)['sku'], 'SKU-B')
        self.assertTrue(db.listing_blocked(LIVE, 1))
        self.assertFalse(paths.current.exports_dir.exists())
        self.assertEqual(self.cli('report', 'mapping'), 0, self.stderr.getvalue())
        self.assertEqual(self.cli('report', 'mapping', '--shop-id', '1'), 0, self.stderr.getvalue())
        book = load_workbook(paths.current.exports_dir / 'shop_1.xlsx', read_only=True)
        try:
            values = {r[1]: r for r in list(book['在架映射明细'].values)[1:]}
            self.assertEqual((values[LIVE][0], values[LIVE][7]), ('新名', 101))
            self.assertNotIn(EXCLUDED, values)
        finally:
            book.close()

    def test_historical_rename_survives_price_book_name_changes(self):
        snapshot(1, [row()])
        map_vcs({LIVE: 'SKU-A'})
        self.assertEqual(self.cli('catalog', 'rename', '--vc', LIVE, '--cn', '新名'), 0)
        write_book([('SKU-B', '运营改名', 25, 'OTHR', 'STOCK-B')])
        self.assertEqual(self.cli('report', 'mapping'), 0, self.stderr.getvalue())
        self.assertEqual(db.resolve_vc(LIVE)['cn'], '运营改名')

    def test_partial_fetch_failure_keeps_previous_shop_and_allows_empty_shop(self):
        snapshot(2, [row(nm=202)])
        previous = document(2)
        shops = [{'id': 1, 'name': '店一'}, {'id': 2, 'name': '店二'}, {'id': 3, 'name': '店三'}]
        with patch.object(products.bcs, 'fetch_shop_list', return_value=shops), \
                patch.object(products.bcs, 'fetch_shop_products', side_effect=[[row()], ValueError('fixture'), []]), \
                patch.object(products.time, 'sleep'):
            self.assertEqual(self.cli('catalog', 'fetch', '--no-sync'), 1)
        self.assertEqual(document(2), previous)
        self.assertEqual(document(3)['rows'], [])
        self.assertEqual([r['id'] for r in shop_summary()['errors']], [2])

    def test_latest_prices_can_fall_back_for_reports_but_not_writes(self):
        from sellerops.persistence.mappings import MappingRepository

        map_vcs({LIVE: 'SKU-A'})
        with patch.object(Path, 'open', side_effect=PermissionError('Excel occupied')):
            # Exercise the explicit access check with unchanged workbook metadata.
            with self.assertRaisesRegex(ValueError, '保存并关闭'):
                MappingRepository.load_price_book(fresh=True)
            self.assertEqual(MappingRepository.load_price_book()[0]['minimum_price'], 10)

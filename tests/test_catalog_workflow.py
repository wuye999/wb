"""SQLite migration, review and mapping exports from the normal CLI."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook, load_workbook

from catalog_fixture import document, map_vcs, mock_non_git_checkout, shop_summary, snapshot, write_book
from sellerops.cli.main import main
from sellerops.core.files import atomic_dump_json
from sellerops.features.catalog import products
from sellerops.features.catalog.mapping_excel import build_xlsx
from sellerops.persistence import db, migrate_legacy, open_api_publish
from sellerops.persistence.mappings import MappingRepository, read_current_price_book, save_price_book
from sellerops.persistence.price_book import inspect_price_book
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
        mock_non_git_checkout(self)
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
        self.assertNotIn('shipping_mode', columns)
        owner = db.resolve_vc(LIVE)
        self.assertEqual((owner['stock_sku'], owner['shipping_mode']), ('OLD-STOCK', None))
        self.assertEqual(owner['minimum_price'], 10)

        [product] = MappingRepository.load_price_book()
        self.assertEqual((product['stock_sku'], product['shipping_mode']), ('STOCK-A', '马帮发货'))
        self.assertEqual(db.resolve_vc(LIVE)['stock_sku'], 'OLD-STOCK')
        book = read_current_price_book()
        with db.connect(write=True) as connection:
            save_price_book(connection, book)
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
        database = Path(paths.current.database)
        before = database.read_bytes()
        self.assertEqual(self.cli('report', 'mapping'), 0, self.stderr.getvalue())
        self.assertEqual(MappingRepository.build_vc_resolver()[0](LIVE), '运营改名')
        self.assertEqual(database.read_bytes(), before)

    def test_old_database_byte_identical_for_readers_and_migrates_atomically_on_write(self):
        snapshot(1, [row(), row(UNKNOWN, 103)])
        map_vcs({LIVE: 'SKU-A', BACKUP: 'SKU-B'})
        with db.connect(write=True) as connection:
            db.set_vc(connection, 'ORPHAN', 'mapped', sku='OLD-UNKNOWN', source='manual')
            for col in db.SNAPSHOT_COLUMNS:
                connection.execute(f'ALTER TABLE shops DROP COLUMN {col}')
            connection.execute('ALTER TABLE products DROP COLUMN name_unique')
            connection.execute('ALTER TABLE products DROP COLUMN shipping_mode')
            connection.execute('ALTER TABLE vc_map DROP COLUMN last_cn')
        database = Path(paths.current.database)
        original = database.read_bytes()
        self.assertEqual(db.resolve_vc(LIVE, include_history=True)['cn'], '原名')
        self.assertIsNone(db.resolve_vc(LIVE)['shipping_mode'])
        for refresh in (False, True):
            if refresh:
                write_book([('SKU-A', '只读看到的新名称', 10, 'TEST', 'STOCK-A', '马帮发货')])
            for command in (('doctor',), ('shop', 'list'), ('catalog', 'product-summary'), ('map', 'candidates'),
                            ('map', 'workbench'), ('map', 'check-mismatch'), ('report', 'mapping'),
                            ('report', 'mapping', '--shop-id', '1'), ('catalog', 'export-products', '--all-shops')):
                with self.subTest(command=command, refresh=refresh):
                    json_flag = () if command[:2] == ('catalog', 'export-products') else ('--json',)
                    self.assertEqual(self.cli(*command, *json_flag), 0, self.stderr.getvalue())
                    self.assertEqual(database.read_bytes(), original)
        self.assertEqual(MappingRepository.build_vc_resolver()[0](LIVE), '只读看到的新名称')
        with self.assertRaisesRegex(RuntimeError, 'rollback'), db.connect(write=True) as connection:
            self.assertTrue(db.has_column(connection, 'vc_map', 'last_cn'))
            self.assertEqual(db.resolve_vc(LIVE, db.vc_entries(connection))['last_cn'], '原名')
            raise RuntimeError('rollback')
        self.assertEqual(database.read_bytes(), original)
        with db.connect(write=True):
            pass
        self.assertEqual(db.resolve_vc(LIVE)['last_cn'], '原名')
        self.assertEqual(db.resolve_vc(BACKUP)['last_cn'], '新名')
        self.assertIsNone(db.resolve_vc('ORPHAN')['last_cn'])
        self.assertEqual(db.resolve_vc('ORPHAN')['status'], 'mapped')
        migrated = database.read_bytes()
        with db.connect(write=True) as connection:
            self.assertTrue(db.has_column(connection, 'products', 'shipping_mode'))
        self.assertEqual(database.read_bytes(), migrated)

    def test_history_lifecycle_keeps_display_and_isolates_current_operations(self):
        from sellerops.domain.models import PublishItem
        from sellerops.features.catalog import selection
        from sellerops.features.orders import feishu_register, matching
        from sellerops.features.publishing.publish_details import resolve_publish_details
        from sellerops.features.publishing.publish_records import submit_once
        from sellerops.features.reports import fbs_delivery, order_stats, order_stats_vc, ratings
        from sellerops.features.support import product_context
        from sellerops.persistence import vc_normalization
        from sellerops.persistence.product_index import NmResolver
        from sellerops.persistence.report_catalog import load_report_catalog

        write_book([('SKU-A', '原名', 10, 'TEST', 'STOCK-A', '马帮发货')])
        snapshot(1, [row(), row(OTHER, 102), row(UNKNOWN, 103)])
        map_vcs({LIVE: 'SKU-A'})
        write_book([('SKU-A', '改名后的旧品', 12, 'TEST', 'STOCK-A', '马帮发货')])
        with patch.object(products.bcs, 'fetch_shop_products', return_value=[row(), row(OTHER, 102)]):
            self.assertEqual(self.cli('sync', '--shop-id', '1', '--no-sync', '--json'), 0, self.stderr.getvalue())
        self.assertEqual(db.resolve_vc(LIVE)['last_cn'], '改名后的旧品')
        self.assertEqual(matching.load_local_mapping()[1][LIVE], 'STOCK-A')
        publish_input = PublishItem(nm_id=101, price=30, vendor_code='BCS-TEST-3000', length=10, width=10,
                                    height=10, weight=.5, cn='改名后的旧品', extra={'card_info': {'imt_name': '测试'}})
        frozen, error = resolve_publish_details(publish_input)
        self.assertIsNone(error)
        self.assertEqual(frozen.extra['seller_sku'], 'SKU-A')

        write_book([('NEW-SKU', '改名后的旧品', 35, 'TEST', 'NEW-STOCK', '马帮发货')])
        with patch.object(products.bcs, 'fetch_shop_products', return_value=[row(), row(OTHER, 102)]):
            self.assertEqual(self.cli('sync', '--shop-id', '1', '--no-sync', '--json'), 0, self.stderr.getvalue())
        self.assertEqual(db.resolve_vc(LIVE)['status'], 'mapped')
        self.assertEqual(db.resolve_vc(LIVE)['sku'], 'SKU-A')
        self.assertEqual(db.resolve_vc(LIVE, include_history=True)['cn'], '改名后的旧品')
        self.assertEqual(NmResolver(1).resolve(101)['cn'], '改名后的旧品')
        self.assertIsNone(db.resolve_vc(LIVE)['minimum_price'])
        self.assertEqual(load_report_catalog().historical_vcs, {LIVE})
        self.assertEqual(self.cli('map', 'candidates', '--json'), 0)
        self.assertNotIn(LIVE, self.stdout.getvalue())
        self.assertEqual(self.cli('doctor', '--json'), 0)
        self.assertEqual(json.loads(self.stdout.getvalue())['data']['pending_vcs'], 1)
        self.assertEqual(self.cli('catalog', 'product-summary', '--json'), 0)
        summary = json.loads(self.stdout.getvalue())['data']
        self.assertEqual((summary['totals']['historical_items'], summary['totals']['unmapped_items']), (1, 1))
        self.assertEqual(summary['groups'][0]['name'], '改名后的旧品')
        for args, file, sheet in [((), '价格映射表.xlsx', '历史归属'),
                                  (('--shop-id', '1'), 'shop_1.xlsx', '在架映射明细')]:
            self.assertEqual(self.cli('report', 'mapping', *args), 0, self.stderr.getvalue())
            workbook = load_workbook(paths.current.exports_dir / file, read_only=True)
            try:
                _, history = list(workbook[sheet].values)
                self.assertIn(LIVE, history)
                self.assertIn('改名后的旧品', history)
                if args:
                    self.assertEqual((history[2], history[-1]), (None, '历史归属'))
            finally:
                workbook.close()

        state, _, book = selection.load_state([1], all_products=True)
        self.assertIsNone(state[LIVE]['sku'])
        self.assertEqual(selection.resolve_filters(SimpleNamespace(name='改名后的旧品'), state, book)[0], [])
        self.assertEqual(selection.resolve_filters(SimpleNamespace(vc=LIVE), state, book)[0], [LIVE])
        self.assertNotIn(LIVE, matching.load_local_mapping()[0])
        order = {'id': 'm1', 'platformOrderId': 'p1', 'shopIdText': '1',
                 'order_ellipsis_other_title': f'数量:{LIVE}*1', 'order_ellipsis_title': '数量:STOCK-A*1'}
        names, stocks, modes = matching.load_local_mapping()
        [classified] = matching.classify([order], names, stocks, modes, {'1': '本店(1)'})
        self.assertEqual(classified['status'], 'NO_VC')
        with (patch.object(feishu_register, 'get_mabang_cred', return_value={'shop_map': {'1': '本店(1)'}}),
              patch.object(feishu_register, 'fetch_pending_orders', return_value=[order]),
              patch('sellerops.integrations.wildberries.client.fetch_canceled_ids', return_value=set())):
            [(fields, reasons)], _ = feishu_register.build_records(SimpleNamespace(scope='pending', days=0, page_size=10))
        self.assertEqual((fields['商品中文名'], fields['库存SKU'], fields['wb编号'], reasons),
                         ('改名后的旧品', None, '101', []))
        context = product_context.load_context()
        with patch.object(product_context, 'fetch_card_json', return_value=None):
            self.assertEqual(context[LIVE]['cn'], '改名后的旧品')
            self.assertEqual(product_context.fetch_product_info(101, LIVE, context)['price'], '')
        result = ratings._match_price_table(LIVE, MappingRepository.load_vc_entries())
        self.assertEqual((result['cn'], result['minimum_price'], result['prefix'], result['dim']),
                         ('改名后的旧品', None, None, None))
        history = {'cn': '改名后的旧品', 'vc': LIVE, 'stock_sku': None}
        current = {'cn': '改名后的旧品', 'vc': OTHER, 'stock_sku': 'NEW-STOCK'}
        self.assertEqual(fbs_delivery.aggregate_by_cn([current, history])[0]['stock_sku'], '')
        self.assertEqual(order_stats.aggregate_by_cn([current, history])[0]['stock_sku'], '')
        self.assertEqual({item['vc']: item['stock_sku'] for item in order_stats_vc.aggregate_by_vc([current, history])},
                         {LIVE: '', OTHER: 'NEW-STOCK'})
        self.assertEqual(vc_normalization.load_catalog().names(LIVE), set())
        invalid, error = resolve_publish_details(publish_input)
        self.assertIsNone(invalid)
        self.assertIn('SKU-A', error)
        send = Mock(return_value={'code': 200})
        with self.assertRaisesRegex(ValueError, '重新预览'):
            submit_once([frozen], [2], send)
        send.assert_not_called()
        with patch('sellerops.features.publishing.publish_records.read_current_price_book',
                   side_effect=AssertionError('短路径无需读取价格表')):
            self.assertEqual(submit_once([PublishItem(nm_id=404, vendor_code='BCS-FREE-3001')], [2], send)['code'], 200)
        send.assert_called_once()

        write_book([('SKU-A', '补回后的名称', 18, 'TEST', '', 'WB发货')])
        with patch.object(products.bcs, 'fetch_shop_products', return_value=[row(), row(OTHER, 102)]):
            self.assertEqual(self.cli('sync', '--shop-id', '1', '--no-sync', '--json'), 0, self.stderr.getvalue())
        revived = db.resolve_vc(LIVE)
        self.assertEqual((revived['sku'], revived['cn'], revived['last_cn'], revived['minimum_price'], revived['shipping_mode']),
                         ('SKU-A', '补回后的名称', '补回后的名称', 18, 'WB发货'))
        self.assertEqual(matching.load_local_mapping()[2][LIVE], 'WB发货')
        self.assertEqual(matching.classify([order], *matching.load_local_mapping(), {'1': '本店(1)'})[0]['status'],
                         matching.STATUS_GROUP_SHIPPING)

    def test_mapping_report_reconciles_only_current_mapped_vcs_and_keeps_real_issues(self):
        write_book([('SKU-A', '示例在架商品', 10), ('SKU-B', '示例历史商品', 20),
                    ('SKU-C', '示例目录商品', 12), ('SKU-D', '示例缺价商品', None),
                    ('SKU-E', '示例在架商品', 15)])
        snapshot(1, [row(), row(OTHER, 102, 202), row(UNKNOWN, 103, 203), row(EXCLUDED, 104, 204)])
        second_shop = row(nm=201, chrt=301)
        second_shop['sizeList'][0]['price'] = 20
        snapshot(2, [second_shop])
        map_vcs({LIVE: 'SKU-A', BACKUP: 'SKU-B', OTHER: 'SKU-D'})
        with db.connect(write=True) as connection:
            db.set_vc(connection, EXCLUDED, 'excluded', note='示例非货盘')

        self.assertEqual(self.cli('report', 'mapping', '--json'), 0, self.stderr.getvalue())
        receipt = json.loads(self.stdout.getvalue())
        with closing(load_workbook(receipt['data']['artifacts']['workbook'], read_only=True)) as book:
            self.assertEqual(book.sheetnames, ['映射总表', '多重映射冲突', '未映射商品', '已排除清单',
                                               '待核查清单', '店铺全量商品', '店铺覆盖矩阵', '多店价格一致性'])
            self.assertEqual({row[1] for row in list(book['映射总表'].values)[1:]}, {LIVE, OTHER})
            header, issue = book['待核查清单'].values
            self.assertEqual(header, ('商品SKU', '产品中文名', '最低售价', '问题说明'))
            self.assertEqual(issue[:3], ('SKU-D', '示例缺价商品', None))
            self.assertIn('《商品价格表.xlsx》的「最低售价」', issue[3])
            self.assertIn(UNKNOWN, {row[1] for row in list(book['未映射商品'].values)[1:]})
            self.assertEqual(list(book['已排除清单'].values)[1][:2], (EXCLUDED, '示例非货盘'))
            self.assertEqual(list(book['多店价格一致性'].values)[1][:5], (LIVE, '示例在架商品', 10, 2, 20))

        self.assertEqual(self.cli('report', 'mapping', '--shop-id', '1', '--json'), 0, self.stderr.getvalue())
        receipt = json.loads(self.stdout.getvalue())
        with closing(load_workbook(receipt['data']['artifacts']['workbook'], read_only=True)) as book:
            pending = list(book['未映射在架商品'].values)
            self.assertEqual(len(pending), 2)
            self.assertIn(UNKNOWN, pending[1])

    def test_mapping_builder_skips_empty_vcs_and_preserves_conflict_and_missing_price(self):
        output = self.folder / 'fixture.xlsx'
        counts = build_xlsx(
            [{'cn': '示例无链接商品', 'vendorCodes': []},
             {'cn': '示例归属一', 'sku': 'SKU-A', 'vendorCodes': [LIVE]}],
            [{'cn': '示例空补充条目'}, {'cn': '示例归属二', 'minimum_price': 20, 'vendorCodes': [LIVE]}],
            [], main_sid=1, out_path=output,
        )
        self.assertEqual(counts, (1, 1, 0, 1))
        with closing(load_workbook(output, read_only=True)) as book:
            self.assertEqual([row[:2] for row in list(book['映射总表'].values)[1:]],
                             [('示例归属一', LIVE), ('示例归属二', LIVE)])
            self.assertEqual(list(book['多重映射冲突'].values)[1][:3], (LIVE, 2, '示例归属一；示例归属二'))
            self.assertEqual([row[:3] for row in list(book['待核查清单'].values)[1:]],
                             [('SKU-A', '示例归属一', None)])

    def test_mapping_report_requires_minimum_price_header_even_with_name_cache(self):
        write_book([('SKU-A', '示例目录商品')], ('卖家SKU', '产品中文名'))
        snapshot(1, [])
        self.assertEqual(len(MappingRepository.load_price_book(require_price=False)), 1)
        self.assertEqual(self.cli('report', 'mapping', '--json'), 1)
        receipt = json.loads(self.stdout.getvalue())
        self.assertFalse(receipt['ok'])
        self.assertIn('HeaderError', self.stderr.getvalue())
        self.assertEqual(self.cli('report', 'mapping'), 1)
        self.assertIn('缺少必需列「最低售价」', self.stderr.getvalue())
        self.assertFalse(paths.current.exports_dir.exists())

    def test_catalog_only_product_still_passes_price_book_validation(self):
        write_book([('SKU-A', '示例目录商品', 20, '', '', 'WB发货')])
        book_path = Path(paths.current.price_book_xlsx)
        inspected = inspect_price_book(book_path)
        self.assertEqual(inspected['status'], 'ready')
        self.assertEqual(inspected['data_rows'], 1)
        self.assertEqual(inspected['issue_counts'], {})
        write_book([('SKU-A', '示例目录商品', '无效价格', '', '', 'WB发货')])
        self.assertEqual(inspect_price_book(book_path)['issue_counts'], {'invalid_price': 1})

    def test_candidate_and_workbench_receipts_guide_only_when_groups_exist(self):
        snapshot(1, [row()])
        commands = [('map', 'candidates'), ('map', 'workbench'), ('catalog', 'mapping')]
        for expected_groups in (1, 0):
            if not expected_groups:
                map_vcs({LIVE: 'SKU-A'})
            for command in commands:
                with self.subTest(command=command, groups=expected_groups):
                    self.assertEqual(self.cli(*command, '--json'), 0, self.stderr.getvalue())
                    receipt = json.loads(self.stdout.getvalue())
                    if expected_groups:
                        self.assertIn('完整 VC' if command[1] == 'candidates' else '打开 HTML', receipt['next_action'])
                    else:
                        self.assertEqual(receipt['next_action'], '')
                        self.assertIn('当前没有待认领商品', receipt['summary'])
                    data = receipt['data']
                    if command[1] == 'candidates':
                        self.assertEqual(len(data['groups']), expected_groups)
                    else:
                        self.assertEqual(data['counts']['groups'], expected_groups)
                        self.assertEqual(data['counts']['vcs'], expected_groups)
                        page = Path(data['artifacts']['html']).read_text(encoding='utf-8')
                        self.assertEqual(page.count('<article class="card"'), expected_groups)
                        self.assertIn('目前没有待认领商品', page)

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
        with patch('openpyxl.load_workbook', side_effect=PermissionError('Excel occupied')):
            with self.assertRaisesRegex(ValueError, '保存并关闭'):
                MappingRepository.load_price_book(fresh=True)
            self.assertEqual(MappingRepository.load_price_book()[0]['minimum_price'], 10)

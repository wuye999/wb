"""Offline round trips for the standard shop exchange sheet."""
from contextlib import redirect_stderr, redirect_stdout
import csv
from io import StringIO
import json
from pathlib import Path
import socket
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from catalog_fixture import map_vcs, snapshot, write_book
from sellerops.cli.main import build_parser, main
from sellerops.features.catalog import export_products as exporter
from sellerops.features.products import warehouse
from sellerops.features.publishing import card_publish, import_publish as importer
from sellerops.persistence import db
from sellerops.settings import paths


def product(vc, nm_id, **fields):
    return {
        'vendorCode': vc, 'nmId': nm_id, 'title': 'Крем', 'repImg': 'https://images.example.test/1.jpg',
        'dimensionsLength': 10, 'dimensionsWidth': 20, 'dimensionsHeight': 30, 'dimensionsWeightBrutto': 0.5,
        'sizeList': [{'chrtId': 1, 'price': 10.05, 'stockList': [{'warehouseId': 11, 'amount': 0}]}],
        **fields,
    }


class TestExchangeScripts(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(paths.configure, paths.current.data_dir)
        self.root = Path(folder.name)
        self.data = paths.configure(self.root / 'data')
        self.output = self.root / 'exchange.xlsx'
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline'))
        self.addCleanup(network.stop)
        self.addCleanup(network.start().assert_not_called)

    def test_cli_parser_enforces_shop_scope_and_import_arguments(self):
        parser = build_parser()
        export = ['catalog', 'export-products']
        import_cmd = ['listing', 'import-publish']
        imported = parser.parse_args([*import_cmd, '--file', 'sheet.csv', '--shops', '042, 43', '--prefix', 'ABCD'])
        self.assertEqual(imported.shops, '42,43')
        self.assertFalse(imported.apply)
        self.assertEqual((imported.price_multiplier, imported.price, imported.stock, imported.limit, imported.interval),
                         (1.0, None, 999, 0, 0.5))
        self.assertTrue(parser.parse_args([*export, '--all-shops']).all_shops)
        for argv in (export, [*export, '--all-shops', '--shops', '41'], [*export, '--shops', '0'],
                     [*import_cmd, '--file', 'sheet.csv', '--shops', '42'], ['ops', 'import-publish']):
            with self.subTest(argv=argv), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                parser.parse_args(argv)
            self.assertEqual(error.exception.code, 2)

    def test_export_contains_only_standard_fields_and_keeps_unmapped_products(self):
        vc = 'BCS-ABCD-17/0000123456-98'
        snapshot(41, [product(vc, 991001, title='=1+1', repImg='https://images.example.test/1.jpg?private=value'),
                      product('SHORT-12', 991002), product('TRASH-00000003', 991003, trashedAt='date'),
                      product('EXCLUDED-00000004', 991004)])
        write_book([('PRIVATE-SELLER', '示例商品', 123.45, 'ABCD', 'PRIVATE-STOCK')])
        map_vcs({vc: 'PRIVATE-SELLER'})
        with db.connect(write=True) as connection:
            db.set_vc(connection, 'EXCLUDED-00000004', 'excluded', source='manual')

        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(['catalog', 'export-products', '--shops', '41', '--out', str(self.output),
                                   '--data-dir', self.data.data_dir]), 0)
        self.assertIn('扫描 4 条，过滤 2 条，去重跳过 0 条，最终导出 2 条', output.getvalue())
        book = load_workbook(self.output)
        try:
            self.assertEqual(book.sheetnames, ['商品列表'])
            sheet = book.active
            self.assertEqual(sheet.freeze_panes, 'A2')
            self.assertEqual(list(sheet.values), [
                ('来源店铺', '商品中文名', 'vendorCode', '源数字码', '本店nmId', '售价(CNY)', '长(cm)', '宽(cm)', '高(cm)',
                 '毛重(kg)', '库存', '俄文标题', '主图链接'),
                ('shop41', '示例商品', vc, '0000123456', 991001, 10.05, 10, 20, 30, 0.5, 0, '=1+1',
                 'https://images.example.test/1.jpg'),
                ('shop41', None, 'SHORT-12', 'SHORT-12', 991002, 10.05, 10, 20, 30, 0.5, 0, 'Крем',
                 'https://images.example.test/1.jpg'),
            ])
            self.assertEqual(sheet['L2'].data_type, 's')
            self.assertEqual(sheet['E2'].hyperlink.target, 'https://www.wildberries.ru/catalog/991001/detail.aspx')
            self.assertEqual(sheet['M2'].hyperlink.target, 'https://images.example.test/1.jpg')
        finally:
            book.close()

    def test_multi_shop_export_picks_complete_rows_then_main_shop_and_lowest_id(self):
        snapshot(41, [product('LOW-12345678', 4101, dimensionsLength=0), product('LOW-23456789', 4102),
                      product('LOW-34567890', 4103), product('ALT-23456789', 4104), product('SHORT-12', 4105)])
        snapshot(42, [product('MID-12345678', 4201), product('MID-34567890', 4202),
                      product('SHORT-12', 4203), product('MID-45678901', 4204)])
        snapshot(43, [product('MAIN-12345678', 4301, dimensionsWeightBrutto=None), product('MAIN-23456789', 4302),
                      product('MAIN-45678901', 4303, sizeList=[]), product('MAIN-56789012', 4304)])
        snapshot(44, [product('ARCHIVED-67890123', 4401)])
        with db.connect(write=True) as connection:
            connection.executemany('UPDATE shops SET shop_name=?, status=? WHERE shop_id=?',
                                   [('低 ID 店', 'active', '41'), ('', 'active', '42'),
                                    ('主店', 'active', '43'), ('归档店', 'archived', '44')])

        command = ['catalog', 'export-products', '--data-dir', self.data.data_dir]
        table = self.root / 'exports' / 'shops_products.xlsx'
        with patch.object(paths, 'MAIN_SHOP', 43):
            for selection in (['--all-shops'], ['--shops', '43,42,41']):
                with self.subTest(selection=selection), redirect_stdout(StringIO()) as output:
                    self.assertEqual(main([*command, *selection]), 0)
                    self.assertIn('店铺 3 家，扫描 13 条，过滤 0 条，去重跳过 7 条，最终导出 6 条', output.getvalue())
                    self.assertIn(str(table), output.getvalue())
                    rows = importer.load_exchange_rows(table)
                    self.assertEqual(len(rows), 6)
                    self.assertEqual({row['源数字码']: (row['来源店铺'], row['vendorCode'], row['本店nmId']) for row in rows}, {
                        '12345678': ('42', 'MID-12345678', 4201),
                        '23456789': ('主店', 'MAIN-23456789', 4302),
                        '34567890': ('低 ID 店', 'LOW-34567890', 4103),
                        '45678901': ('42', 'MID-45678901', 4204),
                        '56789012': ('主店', 'MAIN-56789012', 4304),
                        'SHORT-12': ('低 ID 店', 'SHORT-12', 4105),
                    })

    def test_export_does_not_migrate_old_database_or_overwrite_private_input(self):
        snapshot(41, [])
        with db.connect(write=True) as connection:
            connection.execute('ALTER TABLE products DROP COLUMN shipping_mode')
        database = Path(self.data.database)
        before = database.read_bytes()
        self.assertEqual(exporter.export_products([41], self.output)['count'], 0)
        with self.assertRaisesRegex(ValueError, '缺少本地快照'):
            exporter.export_products([42], self.output)
        with self.assertRaisesRegex(ValueError, '数据目录之外'):
            exporter.export_products([41], self.data.price_book_xlsx)
        self.assertEqual(database.read_bytes(), before)

    def test_xlsx_round_trip_reports_invalid_rows_and_rounds_price(self):
        snapshot(41, [product('BCS-ABCD-123456-76543210', 991001),
                      product('NO-SIZE-123457', 991002, dimensionsHeight=None),
                      product('NO-PRICE-123458', 991003, sizeList=[]),
                      product('NO-ID-123459', None)])
        exporter.export_products([41], self.output)
        rows = importer.load_exchange_rows(self.output)
        items, skipped = importer.build_publish_items(rows, [42, 43], 'TEST', price_multiplier=1.1, stock=0)
        self.assertEqual(items, [{
            'nm_id': 991001, 'vendor_code': 'BCS-TEST-76543210', 'price': 11.06,
            'target_shops': [42, 43], 'stock': 0, 'length': 10, 'width': 20, 'height': 30, 'weight': 0.5,
            'title': 'Крем', 'main_image': 'https://images.example.test/1.jpg', 'cn': '',
        }])
        self.assertEqual([line for line, _ in skipped], [3, 4, 5])
        for (_, reason), field in zip(skipped, ('高(cm)', '售价', 'nmId')):
            self.assertIn(field, reason)

    def test_csv_accepts_reordered_columns_alias_and_all_source_code_priorities(self):
        path = self.root / 'exchange.csv'
        with path.open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(('源数字码', 'vendorCode', '长(cm)', '宽(cm)', '高(cm)', '毛重(kg)', 'nmId', '售价(CNY)'))
            writer.writerows([('00123456', 'LONGER-789012345', 1, 2, 3, 0.5, 991001, 60),
                              ('12', 'PREFIX-41/0007654321-1', 1, 2, 3, 0.5, 991002, ''),
                              ('12', 'SHORT-2', 1, 2, 3, 0.5, 991003, ''),
                              ('', 'BCS-ELSE-00123456', 1, 2, 3, 0.5, 991004, ''),
                              ('', 'SHORT-2', 1, 2, 3, 0.5, 991005, ''),
                              ('', 'SHORT-3', 1, 2, 3, 0.5, 991003, '')])
        rows = importer.load_exchange_rows(path)
        items, skipped = importer.build_publish_items(rows, [42], 'ABCD', price='7.125', price_multiplier=15)
        self.assertEqual([line for line, reason in skipped if '重复' in reason], [5, 6, 7])
        self.assertEqual([(item['vendor_code'], item['price'], item['stock']) for item in items], [
            ('BCS-ABCD-00123456', 7.13, 999), ('BCS-ABCD-0007654321', 7.13, 999), ('BCS-ABCD-991003', 7.13, 999),
        ])
        for prefix in ('A1CD', 'abcd', 'BCS-ABCD'):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, '--prefix'):
                importer.build_publish_items(rows, [42], prefix)

    def test_import_cli_uses_publisher_for_dry_run_duplicate_filter_and_limited_apply(self):
        snapshot(41, [product('SRC-17/12345678-9', 991001), product('SRC-23456789', 991002),
                      product('SRC-34567890', 991003), product('INVALID-45678901', 991004, sizeList=[]),
                      product('INVALID-56789012', 991005, dimensionsWidth=None)])
        snapshot(42, [product('BCS-ELSE-12345678-1', 551001, trashedAt='date')])
        with redirect_stdout(StringIO()):
            self.assertEqual(main(['catalog', 'export-products', '--shops', '41', '--data-dir', self.data.data_dir]), 0)
        table = self.root / 'exports' / 'shop_41_products.xlsx'
        args = ['listing', 'import-publish', '--file', str(table), '--shops', '42', '--prefix', 'TEST', '--limit', '1',
                '--stock', '0', '--data-dir', self.data.data_dir]
        bcs = Mock(base_url='https://bcs.example.test')
        bcs.post.return_value = {'code': 200}
        warehouse.clear_cache()
        self.addCleanup(warehouse.clear_cache)
        with patch.object(card_publish, 'bcs', bcs), \
                patch.object(warehouse.credentials, 'get', return_value=Mock(wb_warehouse_keyword='测试仓')), \
                patch.object(warehouse, 'list_stores', return_value=[{'id': 99, 'name': '测试仓东区'}]) as stores, \
                patch.object(card_publish, 'fetch_card_json', return_value={'data': {'subject_id': 1}}) as details:
            with redirect_stdout(StringIO()) as preview:
                self.assertEqual(main(args), 0)
            self.assertIn('[表格] 有效 3 行，跳过 2 行', preview.getvalue())
            self.assertIn('表格第 5 行，售价', preview.getvalue())
            self.assertIn('表格第 6 行，包装尺寸', preview.getvalue())
            self.assertIn('命中目标店已有同源商品 VC：BCS-ELSE-12345678-1', preview.getvalue())
            self.assertIn('[dry-run] 待提交 1 组，跳过 1', preview.getvalue())
            self.assertIn('已扫描前 2 / 共 3 条有效商品（达到 --limit 提前停止，另有 1 条未扫描）', preview.getvalue())
            self.assertIn('[默认业务仓] 店42 测试仓东区（99）', preview.getvalue())
            self.assertIn('BCS-TEST-23456789', preview.getvalue())
            self.assertNotIn('BCS-TEST-34567890', preview.getvalue())
            bcs.post.assert_not_called()
            details.assert_called_once_with(991002)
            [report] = list(Path(self.data.log_dir).rglob('商品操作跳过_*.csv'))
            self.assertIn(str(report), preview.getvalue())
            with report.open(encoding='utf-8-sig', newline='') as file:
                rows = list(csv.DictReader(file))
            self.assertEqual([row['表格行号'] for row in rows], ['5', '6', '2'])
            self.assertEqual([row['原因码'] for row in rows], ['table_invalid', 'table_invalid', 'target_same_source'])
            self.assertEqual(rows[-1]['命中VC'], 'BCS-ELSE-12345678-1')
            stores.assert_called_once_with(42)
            with redirect_stdout(StringIO()):
                self.assertEqual(main([*args, '--apply']), 0)
            self.assertEqual([call.args for call in stores.call_args_list], [(42,), (42,)])

        bcs.post.assert_called_once()
        payload = bcs.post.call_args.args[1]
        self.assertEqual(payload['shopDatas'][0]['vendorCode'], 'BCS-TEST-23456789')
        self.assertEqual(json.loads(payload['shop']), [{'id': 42, 'warehouseId': 99, 'warehouseQuantity': 0}])

    def test_import_all_invalid_limits_terminal_samples_and_keeps_every_csv_row(self):
        table = self.root / 'invalid.csv'
        table.write_text('nmId\n' + 'bad\n' * 12, encoding='utf8')
        with redirect_stdout(StringIO()) as preview, patch.object(card_publish, 'publish_cards_simple') as publish:
            code = main(['listing', 'import-publish', '--file', str(table), '--shops', '42', '--prefix', 'TEST',
                         '--data-dir', self.data.data_dir])
        self.assertEqual(code, 1)
        self.assertEqual(preview.getvalue().count('[跳过]'), 5)
        publish.assert_not_called()
        [report] = list(Path(self.data.log_dir).rglob('商品操作跳过_*.csv'))
        with report.open(encoding='utf-8-sig', newline='') as file:
            self.assertEqual(len(list(csv.DictReader(file))), 12)

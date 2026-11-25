"""BCS 缺规格、缺库存和异常响应不产生假零值或空店快照；完全离线。"""

from catalog_fixture import document, map_vcs, snapshot, write_book
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from openpyxl import load_workbook

from sellerops.core.errors import PlatformApiError
from sellerops.core.files import atomic_dump_json
from sellerops.domain.snapshot_values import first_price, quantity, stock_values
from sellerops.features.catalog import products, single_shop
from sellerops.features.catalog.mapping_excel import stock_summary, row_for, build_xlsx
from sellerops.features.products import plans
from sellerops.integrations.bcs.client import BCSClient
from sellerops.settings import paths


VC = 'BCS-TEST-100'


def row(sizes=None):
    return {'shopId': 1, 'nmId': 101, 'vendorCode': VC, 'title': '测试商品',
            'sizeList': sizes if sizes is not None else []}


def size(chrt=11, amount=0, price=0):
    return {'chrtId': chrt, 'price': price,
            'stockList': [{'warehouseId': 42, 'amount': amount}]}


class TestMissingSnapshotValues(unittest.TestCase):
    def test_no_or_incomplete_specs_never_expose_placeholder_zero(self):
        for sizes in (None, [], {}, [None], [{}], [size(None)], [size(False)],
                      [size(), {}], [size(), size()]):
            raw = dict(row(), sizeList=sizes)
            with self.subTest(sizes=sizes):
                self.assertEqual(stock_summary(raw), '')
                self.assertIsNone(stock_values(raw))
                self.assertIsNone(first_price(raw))
        raw = row([size()])
        self.assertEqual(stock_summary(raw), '0')
        self.assertEqual(first_price(raw), 0)

    def test_partial_inventory_and_invalid_amount_are_unknown(self):
        for raw in (row([size(), {'chrtId': 12, 'price': 10, 'stockList': []}]),
                    row([dict(size(), stockList=[{'warehouseId': 42, 'amount': 0}] * 2)]),
                    row([dict(size(), stockList=[{'amount': 0}])])):
            self.assertEqual(stock_summary(raw), '')
        for value in (None, '', False, True, -1, .5, float('nan'), 'bad', '-1'):
            with self.subTest(value=value):
                self.assertIsNone(quantity(value))
                self.assertEqual(stock_summary(row([size(amount=value)])), '')
        self.assertEqual(stock_summary(row([size(amount='0'), size(12, amount='7')])), '0, 7')
        for value in ('', 'bad', False, -1, float('nan'), 10 ** 500):
            self.assertIsNone(first_price(row([size(price=value)])))

    def test_stock_plan_reports_unknown_instead_of_zero_and_never_sums_duplicates(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = patch.object(paths, 'current', paths.DataPaths(Path(directory.name)))
        context.start()
        self.addCleanup(context.stop)
        for raw_size in (size(amount=None), size(amount=''), size(amount=False),
                         dict(size(), stockList=[{'warehouseId': 99, 'amount': 0}]),
                         dict(size(), stockList=[{'warehouseId': 42, 'amount': 0}] * 2)):
            with patch.object(plans, 'load_shop_rows', return_value={VC: row([raw_size])}), \
                    patch.object(plans.warehouse, 'resolve_warehouse', return_value=42):
                item = plans.plan_stock([VC], [1], {}, 0)[0]['items'][0]
            self.assertIsNone(item['cur_amt'])
            self.assertEqual(item['warehouseId'], 42)
        with patch.object(plans, 'load_shop_rows', return_value={VC: row([size(amount='0')])}), \
                patch.object(plans.warehouse, 'resolve_warehouse', return_value=42):
            item = plans.plan_stock([VC], [1], {}, 9)[0]['items'][0]
        self.assertEqual(item['cur_amt'], 0)
        for sizes in ([], [size(None)], [size(), size()]):
            with patch.object(plans, 'load_shop_rows', return_value={VC: row(sizes)}), \
                    patch.object(plans.warehouse, 'resolve_warehouse', return_value=42):
                for planner, args in ((plans.plan_stock, (0,)), (plans.plan_trash, ())):
                    plan = planner([VC], [1], {}, *args)[0]
                    self.assertEqual(plan['items'], [])
                    self.assertEqual(len(plan['blocked_items']), 1)


class TestBCSProductResponse(unittest.TestCase):
    def test_missing_rows_partial_counts_and_wrong_shop_are_errors(self):
        client = BCSClient()
        bad = [None, [], {'code': 500}, {'code': 200}, {'code': 200, 'rows': None},
               {'code': 200, 'rows': {}}, {'code': 200, 'rows': [None]},
               {'code': 200, 'rows': [], 'total': 1},
               {'code': 200, 'rows': [], 'total': False},
               {'code': 200, 'rows': [dict(row(), shopId=2)], 'total': 1}]
        with patch.object(BCSClient, 'base_url', new_callable=PropertyMock, return_value='https://example.invalid'):
            for response in bad:
                with self.subTest(response=response), patch.object(client, 'get', return_value=response), \
                        self.assertRaises(PlatformApiError):
                    client.fetch_shop_products(1)
            for response in ({'code': 200, 'rows': [], 'total': 0},
                             {'code': 200, 'rows': [row()], 'total': '1'}):
                with patch.object(client, 'get', return_value=response) as request:
                    self.assertEqual(client.fetch_shop_products(1), response['rows'])
                    self.assertIn('pageNum=1&pageSize=10000', request.call_args.args[0])


class TestBCSWorkbookAndFetch(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        previous = paths.current.data_dir
        self.addCleanup(paths.configure, previous)
        self.data = Path(folder.name)
        paths.configure(self.data)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.output = io.StringIO()
        stack.enter_context(redirect_stdout(self.output))
        stack.enter_context(redirect_stderr(self.output))
        stack.enter_context(patch.object(BCSClient, 'base_url', new_callable=PropertyMock,
                                       return_value='https://example.invalid'))

    def run_cli(self, *args):
        from sellerops.cli.main import main
        return main(['--data-dir', str(self.data), *args])

    def test_bcs_fetch_warns_about_incomplete_data_and_export_preserves_real_zero(self):
        raw = [row([size(None)]), dict(row([size()]), vendorCode='REAL-ZERO', nmId=102)]
        with patch.object(products.bcs, 'get', return_value={'code': 200, 'rows': raw, 'total': 2}), \
                patch.object(products.bcs, 'sync_shop') as sync:
            self.assertEqual(self.run_cli('catalog', 'fetch', '--source', 'bcs', '--shop-id', '1', '--no-sync'), 0)
            sync.assert_not_called()
        doc = document(1)
        self.assertEqual(doc['rows'], raw)
        self.assertEqual(doc['warnings'], {'missingSpecProducts': 1, 'missingStockProducts': 1,
                                          'missingPriceProducts': 1})
        self.assertIn('规格不完整的商品=1', self.output.getvalue())
        write_book([('S1', '\u672a\u77e5\u89c4\u683c', 10, '', ''), ('S2', '\u771f\u5b9e\u96f6', 10, '', '')])
        map_vcs({VC: 'S1', 'REAL-ZERO': 'S2'})
        report = self.data / 'report.xlsx'
        single_shop.build_single_shop_xlsx(1, report)
        book = load_workbook(report, read_only=True, data_only=True)
        try:
            data = list(book['在架映射明细'].iter_rows(values_only=True))
            headers = list(data[0])
            for name in ('本店售价(CNY)', '本店库存'):
                self.assertIsNone(data[1][headers.index(name)])
            self.assertEqual(data[2][headers.index('本店售价(CNY)')], 0)
            self.assertEqual(data[2][headers.index('本店库存')], '0')
        finally:
            book.close()

    def test_invalid_bcs_response_cannot_clear_existing_snapshot(self):
        snapshot(1, [row([size()])])
        before = document(1)
        for response in ({'code': 200}, {'code': 200, 'rows': [], 'total': 1}):
            with patch.object(products.bcs, 'get', return_value=response):
                self.assertEqual(self.run_cli('catalog', 'fetch', '--source', 'bcs', '--shop-id', '1', '--no-sync'), 1)
            self.assertEqual(document(1), before)

    def test_total_table_never_borrows_other_shop_stock_for_unknown_primary(self):
        for raw_primary in (row([dict(size(price=12), stockList=[])]), row([size(None)])):
            primary = dict(raw_primary, vc=VC, price=first_price(raw_primary))
            # 无规格的主店行会被主店候选加载器排除，但必须保留该店的未知值。
            bcs_rows = [primary] if primary['price'] is not None else []
            coverage = {VC: {1: {'stock': '', 'price': first_price(raw_primary), 'nmId': 101},
                             2: {'stock': 0, 'price': 20, 'nmId': 202}}}
            build_xlsx([], [{'cn': '商品', 'minimum_price': 10, 'vendorCodes': [VC]}], bcs_rows,
                       shop_coverage=coverage, shops_meta=[{'id': 1, 'name': '主店'}, {'id': 2, 'name': '其他店'}],
                       main_sid=1, out_path=self.data / 'report.xlsx')
            book = load_workbook(self.data / 'report.xlsx', read_only=True, data_only=True)
            try:
                rows = list(book['映射总表'].iter_rows(values_only=True))
                headers = list(rows[0])
                self.assertIsNone(rows[1][headers.index('库存')])
                self.assertEqual(rows[1][headers.index('WB商品码')], 101)
                matrix = list(book['店铺覆盖矩阵'].iter_rows(values_only=True))
                self.assertIsNone(matrix[1][2])
                self.assertEqual(matrix[1][3], 0)
            finally:
                book.close()
        data = row_for({'cn': '商品'}, VC, {'_stock': 0}, stock_summary_fn=lambda value: '999')
        self.assertEqual(data[6], 0)

    def test_mismatch_prices_preserve_real_zero_and_ignore_spec_placeholder_zero(self):
        from sellerops.features.catalog import mapping, mismatch_check
        good = dict(row([size()]), vendorCode='REAL-ZERO', nmId=102)
        fraction = dict(row([size(price=.5)]), vendorCode='FRACTION', nmId=103)
        rows = [row([size(None)]), good, fraction]
        snapshot(1, rows)
        write_book([('SKU1', '示例商品', 10)])
        map_vcs({item['vendorCode']: 'SKU1' for item in rows})
        self.assertEqual({item['vc']: item['price'] for item in mapping.priced_rows(rows)},
                         {'REAL-ZERO': 0, 'FRACTION': .5})
        self.assertEqual({item['vc']: item['price'] for item in mismatch_check.load_mapping_rows()},
                         {VC: None, 'REAL-ZERO': 0, 'FRACTION': .5})


if __name__ == '__main__':
    unittest.main()

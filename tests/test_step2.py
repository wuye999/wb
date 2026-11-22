"""WorkBuddy 入门命令和固定计划单的离线验收。"""
import io
import json
import tempfile
import unittest
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from catalog_fixture import document, snapshot, write_book
from sellerops.cli.main import main
from sellerops.features.catalog import products
from sellerops.features.products import operation_plan
from sellerops.features.system import doctor
from sellerops.integrations.wildberries import client as wb
from sellerops.persistence import db
from sellerops.settings import paths


A = 'BCS-AAAA-ozon-card-123456-1'
B = 'BCS-RAND-x/123456-4'
C = 'BCS-OTHER-334455'


def row(vc, nm, chrt):
    return {'vendorCode': vc, 'nmId': nm, 'title': 'fixture', 'repImg': 'https://example.invalid/a.png',
            'discount': 20, 'sizeList': [{'chrtId': chrt, 'price': 38}]}


class TestStep2(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(paths.configure, paths.current.data_dir)
        self.root = Path(temp.name)
        self.data = self.root / 'data'
        paths.configure(self.data)

    def cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch('sys.stdin', io.StringIO(stdin or '')):
            code = main(['--data-dir', str(self.data), *argv, '--json'])
        return code, json.loads(out.getvalue()), err.getvalue()

    def populate(self):
        write_book([('S1', '旅行杯', 38, 'AAAA', 'STOCK1'), ('S2', '旅行杯', 39, 'BBBB', 'STOCK2')])
        snapshot(1, [row(A, 101, 1001), row(B, 102, 1002), row(C, 103, 1003)], fetchedAt='2026-01-01')
        snapshot(2, [row(A, 201, 2001)], fetchedAt='2026-01-02')

    def confirm(self, *rows):
        return self.cli('map', 'batch-set', stdin=json.dumps(list(rows)))

    def test_doctor_default_has_no_auth_or_file_mutation(self):
        with patch.object(doctor, 'online_checks', side_effect=AssertionError('offline')) as online:
            code, reply, _ = self.cli('doctor')
        online.assert_not_called()
        self.assertEqual(code, 0)
        self.assertFalse(reply['data']['online_checked'])
        self.assertFalse(self.data.exists())
        self.populate()
        (self.data / 'credentials.json').write_text('{}', encoding='utf-8')
        with patch.object(doctor, 'online_checks', return_value={'bcs': 'authentication_failed'}) as online:
            code, reply, _ = self.cli('doctor', '--check-online')
        self.assertEqual(code, 0)
        self.assertEqual(reply['data']['pending_vcs'], 3)
        self.assertEqual(reply['data']['online'], {'bcs': 'authentication_failed'})
        online.assert_called_once_with()

    def test_sync_alias_reports_missing_and_archive_restore_preserves_rows(self):
        self.populate()
        before = document(2)
        with patch.object(products.bcs, 'fetch_shop_list', return_value=[{'id': 1, 'name': '新名字'}]), \
                patch.object(products.bcs, 'fetch_shop_products', return_value=[row(A, 101, 1001)]), \
                patch.object(products.bcs, 'sync_shops_parallel') as remote_sync:
            code, reply, _ = self.cli('sync', '--all', '--no-sync')
        self.assertEqual(code, 0)
        remote_sync.assert_not_called()
        self.assertEqual([row['shop_id'] for row in reply['data']['missing_shops']], ['2'])
        self.assertEqual(document(2), before)
        self.assertEqual(self.cli('shop', 'archive', '--shop-id', '2')[0], 0)
        self.assertIsNone(document(2))
        with patch.object(products.bcs, 'fetch_shop_products') as read:
            self.assertEqual(self.cli('sync', '--shop-id', '2', '--no-sync')[0], 1)
        read.assert_not_called()
        self.assertEqual(self.cli('shop', 'restore', '--shop-id', '2')[0], 0)
        self.assertEqual(document(2), before)

    def test_candidate_group_batch_is_atomic_and_excel_is_six_columns(self):
        self.populate()
        _, reply, _ = self.cli('map', 'candidates', '--limit', '1')
        [group] = reply['data']['groups']
        self.assertCountEqual(group['vcs'], [A, B])
        self.assertEqual(len(next(i for i in group['items'] if i['vc'] == A)['shops']), 2)
        self.assertEqual(self.confirm({'vc': A, 'action': 'map', 'sku': 'S1'},
                                      {'vc': B, 'action': 'map', 'cn': '旅行杯'})[0], 1)
        self.assertEqual(db.resolve_vc(A)['status'], 'pending')
        self.assertEqual(self.confirm({'vc': A, 'action': 'map', 'sku': 'S1', 'source': 'prefix'},
                                      {'vc': C, 'action': 'block', 'shops': ['2']})[0], 0)
        self.assertEqual(db.resolve_vc(A)['source'], 'prefix')
        self.assertTrue(db.listing_blocked(C, 2))
        self.assertFalse(db.listing_blocked(C, 1))
        file = self.root / 'pending.xlsx'
        self.assertEqual(self.cli('map', 'export-pending', '--out', str(file))[0], 0)
        book = load_workbook(file)
        sheet = book.active
        self.assertEqual(sheet.max_column, 6)
        for cells in list(sheet.rows)[1:]:
            if cells[0].value == B:
                cells[5].value = '排除'
        book.save(file)
        book.close()
        self.assertEqual(self.cli('map', 'import-excel', str(file))[0], 0)
        self.assertEqual(db.resolve_vc(B)['status'], 'excluded')
        self.assertEqual(db.resolve_vc(C)['status'], 'pending')

    def preview_price(self):
        self.populate()
        self.assertEqual(self.confirm({'vc': A, 'action': 'map', 'sku': 'S1'})[0], 0)
        code, reply, error = self.cli('ops', 'price', '--shops', '1,2',
                                      '--sku', 'S1', '--price', '55')
        self.assertEqual(code, 0, error)
        return reply['plan_id']

    def test_public_price_submission_freezes_shop_card_and_price(self):
        with patch.object(wb, 'request') as sent:
            plan_id = self.preview_price()
            sent.assert_not_called()
        snapshot(1, [row(A, 901, 9001)])
        snapshot(2, [row(A, 902, 9002)])
        cred = Mock(root_version='fixture')
        cred.wb_shop.side_effect = lambda sid: {'shopId': sid}
        with patch.object(wb.credentials, 'get', return_value=cred), \
                patch.object(wb, 'make_session', side_effect=lambda shop, *_: Mock(sid=shop['shopId'])), \
                patch.object(wb, 'request', return_value={'error': False, 'data': {'id': 33}}) as send, \
                patch.object(wb.time, 'sleep'):
            code, reply, err = self.cli('apply', '--plan', plan_id)
            self.assertEqual(code, 0, (reply, err))
            self.assertEqual(reply['data']['status'], 'done')
            self.assertEqual({(call.args[0].sid, item['nmID'], item['price'])
                              for call in send.call_args_list for item in call.kwargs['json']['data']},
                             {(1, 101, 55), (2, 201, 55)})
            calls = send.call_count
            repeated = self.cli('apply', '--plan', plan_id)
            self.assertEqual(repeated[0], 1)
            self.assertEqual(send.call_count, calls)
        out = io.StringIO()
        with redirect_stderr(out), redirect_stdout(out):
            self.assertEqual(main(['--data-dir', str(self.data), 'product', 'price', '--shops', '1',
                                   '--vc', A, '--price', '5', '--apply']), 1)
        self.assertIn('apply --plan', out.getvalue())

    def test_stock_and_trash_plan_preserve_exact_frozen_spec_and_warehouse(self):
        from sellerops.features.products import plans, stocks, trash
        from sellerops.integrations.wildberries import stocks as wb_stocks

        self.populate()
        cred = Mock(root_version='fixture')
        cred.wb_shop.side_effect = lambda sid: {'shopId': sid}
        with patch.object(plans.warehouse, 'resolve_warehouse', return_value=42), \
                patch.object(wb.credentials, 'get', return_value=cred), \
                patch.object(wb, 'make_session', return_value=Mock()), \
                patch.object(wb_stocks, 'post_stocks', return_value={'error': False}) as post, \
                patch.object(wb.time, 'sleep'):
            rc, draft, err = self.cli('ops', 'stock', '--shops', '1', '--vc', A, '--amount', '7')
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.cli('apply', '--plan', draft['plan_id'])[0], 0)
            self.assertEqual(post.call_args.args[1:], (42, [{'chrtId': 1001, 'amount': 7}]))
            rc, draft, err = self.cli('ops', 'trash', '--shops', '1', '--vc', A)
            self.assertEqual(rc, 0, err)
            snapshot(1, [row(A, 901, 9001)])
            events = []
            def clear(shop, warehouse, items, **kw):
                events.append(('zero', warehouse, items[0]['chrtId']))
                return {'ok': 1, 'fail': 0,
                        'details': [{'chrtId': items[0]['chrtId'], 'amount': 0, 'ok': True}]}
            def move(shop, ids, **kw):
                events.append(('trash', ids))
                return {'details': [{'nmID': ids[0], 'status': 'accepted', 'msg': ''}]}
            with patch.object(stocks, 'set_stock', side_effect=clear), patch.object(trash, 'move_nms', side_effect=move):
                self.assertEqual(self.cli('apply', '--plan', draft['plan_id'])[0], 0)
            self.assertEqual(events, [('zero', 42, 1001), ('trash', [101])])

    def test_changed_workbook_price_stops_an_old_book_price_plan(self):
        self.populate()
        self.assertEqual(self.confirm({'vc': A, 'action': 'map', 'sku': 'S1'})[0], 0)
        code, reply, error = self.cli('ops', 'price', '--shops', '1', '--sku', 'S1')
        self.assertEqual(code, 0, error)
        write_book([('S1', 'fixture', 88, 'AAAA', '')])
        with patch.object(wb, 'request') as send:
            rc, applied, _ = self.cli('apply', '--plan', reply['plan_id'])
        self.assertEqual((rc, applied['data']['status']), (1, 'partial'))
        send.assert_not_called()

    def test_bulk_publish_apply_uses_frozen_warehouse_price_and_task_receipt(self):
        from sellerops.domain.models import PublishItem
        from sellerops.features.products import warehouse
        from sellerops.features.publishing import bulk_publish

        self.populate()
        item = PublishItem(101, price=59, vendor_code=A, prefix='BCS-AAAA',
                           length=10, width=20, height=30, weight=.2, stock=0, target_shops=[1])
        with patch.object(bulk_publish, 'resolve_publish_details', return_value=(item, None)), \
                patch.object(bulk_publish.bcs, 'fetch_shop_list', return_value=[{'id': 1, 'name': 'shop'}]), \
                patch.object(bulk_publish, 'resolve_warehouse', return_value=42), \
                patch.object(bulk_publish, 'vc_exists_in_shop', return_value=False), \
                patch.object(warehouse, 'resolve_warehouse', return_value=42), \
                patch.object(bulk_publish.bcs, 'batch_push_products',
                             return_value={'code': 200, 'data': {'taskId': 'fixture-task'}}) as push:
            rc, draft, error = self.cli('ops', 'publish', '101', '--shops', '1', '--stock', '0')
            self.assertEqual(rc, 0, (draft, error))
            push.assert_not_called()
            rc, applied, error = self.cli('apply', '--plan', draft['plan_id'])
            self.assertEqual(rc, 0, (applied, error))
        self.assertEqual(push.call_args.args[0], [{'id': 1, 'warehouseId': 42, 'warehouseQuantity': 0}])
        self.assertEqual(push.call_args.args[1][0]['price'], '59')
        self.assertEqual(applied['data']['receipt']['items'][0]['task_id'], 'fixture-task')

    def test_concurrent_claim_and_unknown_block_replay(self):
        plan_id = self.preview_price()
        entered, release = Event(), Event()

        def send(route, opts):
            self.assertEqual(route, 'price')
            with db.connect() as conn:
                self.assertEqual(conn.execute('SELECT status FROM plans WHERE plan_id=?', (plan_id,)).fetchone()[0],
                                 'submitting')
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            opts.outcome = {'receipts': [{'status': 'unknown'}]}
            return 1

        with ThreadPoolExecutor() as pool, redirect_stdout(io.StringIO()):
            first = pool.submit(operation_plan.apply, Namespace(plan=plan_id), send)
            try:
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaisesRegex(ValueError, 'submitting'):
                    operation_plan.apply(Namespace(plan=plan_id), send)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), 1)
        with self.assertRaisesRegex(ValueError, 'unknown'):
            operation_plan.apply(Namespace(plan=plan_id), send)

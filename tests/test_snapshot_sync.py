"""Offline snapshot receipt, migration, and BCS scheduling regressions."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import time
from unittest import TestCase
from unittest.mock import patch

import requests

from catalog_fixture import document, map_vcs, mock_non_git_checkout, snapshot, write_book
from sellerops.cli.main import main
from sellerops.core.errors import AuthenticationError, NetworkError
from sellerops.features.catalog import products, shops
from sellerops.integrations.bcs.budget import RequestBudget, SyncInterrupted
from sellerops.integrations.bcs.client import BCSClient, OpenAPIShop
from sellerops.integrations.bcs.session import BCSSession
from sellerops.persistence import db
from sellerops.settings import paths
from test_bcs_auth import reply


RECEIPT_KEYS = {'source', 'fetchedAt', 'shops', 'fetched', 'sync_warnings', 'errors', 'missing_shops', 'prefix_mapping'}
OLD_STAMP = '2026-01-01 10:00:00'
STORES = [{'id': sid, 'name': f'Shop {sid}'} for sid in (1, 2, 3)]


class TestSnapshotSync(TestCase):
    def setUp(self):
        mock_non_git_checkout(self)
        self.data = Path(self.enterContext(TemporaryDirectory()))
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(self.data)
        self.client = BCSClient(BCSSession('fixture', 'limit', 'Admin-Token=fixture; Limit-Key=limit'))
        self.enterContext(patch.object(products, 'bcs', self.client))
        self.enterContext(patch('time.sleep'))

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['--data-dir', str(self.data), *args, '--json'])
        return code, json.loads(out.getvalue()), err.getvalue()

    def shop_row(self, sid):
        with db.connect(read_only=True) as connection:
            row = connection.execute('SELECT * FROM shops WHERE shop_id = ?', (str(sid),)).fetchone()
            return dict(row) if row else None

    def test_unique_prefix_uses_only_successful_active_snapshot_and_preserves_review(self):
        write_book([('A', '前缀命中', None, 'tEsT'), ('B', '前缀冲突一', 10, 'DUPE'),
                    ('C', '前缀冲突二', 20, 'DUPE'), ('D', '非 ASCII', 22, 'ſEST'),
                    ('E', '人工归属', 30, 'OTHR'), ('', '无SKU唯一中文名', 40, 'NAME')])
        matched = 'BCS-TEST-ozon-card-10000001-2'
        named = 'BCS-naME-supplier/10000002'
        blocked = 'BCS-TEST-10000003'
        blocked_shop = 'BCS-TEST-10000004'
        excluded = 'BCS-TEST-10000005'
        trash = 'BCS-TEST-10000006'
        unsynced = 'BCS-TEST-10000007'
        archived = 'BCS-TEST-10000008'
        old_failed = 'BCS-TEST-10000009'
        protected = {f'BCS-TEST-1000001{i}': source for i, source in enumerate(('manual', 'ai', 'publish', 'prefix'))}
        for vc, source in protected.items():
            map_vcs({vc: 'E'}, source=source)
        with db.connect(write=True) as connection:
            db.set_vc(connection, blocked, 'pending', block_listing='ALL', note='旧禁上架')
            db.set_vc(connection, blocked_shop, 'pending', block_listing='2', note='单店禁上架')
            db.set_vc(connection, excluded, 'excluded', source='manual', note='非货盘')
            db.set_vc(connection, matched, 'pending', block_listing='', note='审核备注')
            connection.execute('UPDATE vc_map SET source_code=? WHERE vc=?', ('原源数字段', matched))
        snapshot(3, [{'vendorCode': archived}])
        self.assertEqual(self.cli('shop', 'archive', '--shop-id', '3')[0], 0)
        snapshot(4, [{'vendorCode': old_failed}])
        snapshot(5, [{'vendorCode': unsynced}])
        preserve = {vc: db.resolve_vc(vc) for vc in (blocked, blocked_shop, excluded, *protected)}
        rows = [{'vendorCode': vc} for vc in (
            matched, blocked, blocked_shop, excluded, *protected, 'BCS-DUPE-1', 'BCS-SEST-1', 'BCS-TEST-')]
        rows.append({'vendorCode': trash, 'trashedAt': '2026-01-01'})
        with (patch.object(self.client, 'fetch_shop_list', return_value=[*STORES, {'id': 4, 'name': '失败'}]),
              patch.object(self.client, 'fetch_shop_products', side_effect=[
                  rows, [{'vendorCode': matched}, {'vendorCode': named}], NetworkError('fixture')]) as read,
              patch.object(products, 'read_current_price_book', wraps=products.read_current_price_book) as book):
            code, result, _ = self.cli('catalog', 'fetch', '--no-sync')
        self.assertEqual(code, 1)
        book.assert_called_once_with(strict_identity=True)
        self.assertEqual([call.args[0] for call in read.call_args_list], [1, 2, 4])
        receipt = result['data']
        self.assertEqual([(row['id'], row['prefix_mapped']) for row in receipt['fetched']], [(1, 1), (2, 1)])
        self.assertEqual(receipt['prefix_mapping'], {'mapped': 2, 'skipped_shops': [], 'skipped_vcs': {
            'blocked': 2, 'ambiguous_prefix': 1, 'unmatched_prefix': 1, 'invalid_vc_prefix': 1}})
        self.assertEqual([row['shop_id'] for row in receipt['missing_shops']], ['5'])
        owner = db.resolve_vc(matched)
        self.assertEqual(tuple(owner[key] for key in ('sku', 'last_cn', 'status', 'source', 'note',
                                                       'source_code', 'block_listing')),
                         ('A', '前缀命中', 'mapped', 'prefix', '审核备注', '原源数字段', ''))
        self.assertEqual(db.resolve_vc(named)['sku'], '无SKU唯一中文名')
        for vc, before in preserve.items():
            self.assertEqual(db.resolve_vc(vc), before)
        for vc in (trash, unsynced, archived, old_failed, 'BCS-DUPE-1', 'BCS-SEST-1', 'BCS-TEST-'):
            self.assertEqual(db.resolve_vc(vc)['status'], 'pending')
        with patch.object(self.client, 'fetch_shop_products', return_value=rows):
            self.assertEqual(self.cli('sync', '--shop-id', '1', '--no-sync')[1]['data']['prefix_mapping']['mapped'], 0)

    def test_unusable_price_books_leave_snapshot_pending_and_cache_unchanged(self):
        vc = 'BCS-TEST-10000001'
        mode = '马帮发货'
        write_book([('OLD', '旧缓存', 10, 'TEST', 'STOCK', mode)])
        map_vcs({'BCS-TEST-11111111': 'OLD'})
        book_path = Path(paths.current.price_book_xlsx)
        headers = ('卖家SKU', '产品中文名', '最低售价', 'vendorCode前缀码', 'vendorCode前缀码')
        cases = [
            ([], ('卖家SKU',), None, '缺少必需列'),
            ([('A', '同SKU一', 10, 'TEST'), ('A', '同SKU二', 10, 'OTHR')], None, None, '重复卖家 SKU'),
            ([('A', '', 10, 'TEST')], None, None, '无效商品键'),
            ([('', '', 10, 'TEST')], None, None, '缺少卖家 SKU'),
            ([('A', '有效', 10, 'TEST', 'OTHR')], headers, None, '重复列'),
            (None, None, None, '缺失'),
            ([('A', '有效', 10, 'TEST')], None, PermissionError(), '被占用'),
            ([('A', '有效', 10, 'TEST')], None, b'not an xlsx', '无法解析'),
        ]
        for items, labels, failure, reason in cases:
            with self.subTest(reason=reason):
                if items is None:
                    book_path.unlink()
                else:
                    write_book(items, **({'headers': labels} if labels else {}))
                if isinstance(failure, bytes):
                    book_path.write_bytes(failure)
                with patch.object(self.client, 'fetch_shop_products', return_value=[{'vendorCode': vc}]):
                    if isinstance(failure, Exception):
                        with patch('openpyxl.load_workbook', side_effect=failure):
                            code, result, _ = self.cli('sync', '--shop-id', '1', '--no-sync')
                    else:
                        code, result, _ = self.cli('sync', '--shop-id', '1', '--no-sync')
                self.assertEqual(code, 0)
                data = result['data']
                self.assertEqual(data['fetched'][0]['prefix_mapped'], 0)
                self.assertIn(reason, data['prefix_mapping']['skipped_shops'][0]['reason'])
                self.assertEqual(document(1)['rows'][0]['vendorCode'], vc)
                self.assertEqual(db.resolve_vc(vc)['status'], 'pending')
                with db.connect() as connection:
                    self.assertEqual([row[0] for row in connection.execute('SELECT sku FROM products')], ['OLD'])

    def test_valid_empty_price_book_clears_current_cache_without_removing_history(self):
        old, new = 'BCS-TEST-1', 'BCS-TEST-2'
        write_book([('A', '已归属', 10, 'TEST')])
        map_vcs({old: 'A'})
        write_book([])
        with patch.object(self.client, 'fetch_shop_products', return_value=[{'vendorCode': old}, {'vendorCode': new}]):
            code, result, _ = self.cli('sync', '--shop-id', '1', '--no-sync')
        self.assertEqual(code, 0)
        self.assertEqual(result['data']['prefix_mapping']['skipped_shops'], [])
        self.assertEqual(result['data']['prefix_mapping']['mapped'], 0)
        self.assertEqual(db.resolve_vc(old, include_history=True)['cn'], '已归属')
        self.assertFalse(db.resolve_vc(old)['current_product'])
        self.assertEqual(db.resolve_vc(new)['status'], 'pending')

    def test_book_changed_inside_shop_save_keeps_first_result_and_rolls_back_second_cache(self):
        first, second = 'BCS-TEST-1', 'BCS-TEST-2'
        write_book([('A', '本轮商品', 10, 'TEST')])
        save = products.save_price_book
        calls = 0

        def save_then_change(connection, book):
            nonlocal calls
            calls += 1
            if calls == 2:
                connection.execute('DELETE FROM products')
            save(connection, book)
            if calls == 2:
                write_book([('B', '保存时已经另存新价格表', 80, 'TEST')])

        with (patch.object(self.client, 'fetch_shop_list', return_value=STORES[:2]),
              patch.object(self.client, 'fetch_shop_products', side_effect=[
                  [{'vendorCode': first}], [{'vendorCode': second}]]),
              patch.object(products, 'save_price_book', side_effect=save_then_change)):
            code, result, _ = self.cli('sync', '--all', '--no-sync')
        self.assertEqual(code, 0)
        receipt = result['data']
        self.assertEqual([item['prefix_mapped'] for item in receipt['fetched']], [1, 0])
        self.assertEqual(receipt['prefix_mapping']['skipped_shops'], [{'id': 2, 'reason': '商品价格表版本已变化'}])
        self.assertEqual(db.resolve_vc(first)['sku'], 'A')
        self.assertEqual(db.resolve_vc(second)['status'], 'pending')
        self.assertEqual(document(2)['total'], 1)
        with db.connect() as connection:
            self.assertEqual([row[0] for row in connection.execute('SELECT sku FROM products')], ['A'])

    def test_sqlite_prefix_write_failure_rolls_back_snapshot_and_cache(self):
        old, new = 'BCS-TEST-1', 'BCS-TEST-2'
        snapshot(1, [{'vendorCode': old}])
        write_book([('A', '旧名称', 10, 'TEST')])
        map_vcs({old: 'A'})
        write_book([('A', '新名称', 12, 'TEST')])
        with db.connect(write=True) as connection:
            connection.execute("""
                CREATE TRIGGER prefix_write_failure BEFORE UPDATE OF status ON vc_map
                WHEN new.source = 'prefix' BEGIN SELECT RAISE(ABORT, 'fixture DB failure'); END
            """)
        previous = document(1)
        with patch.object(self.client, 'fetch_shop_products', return_value=[{'vendorCode': new}]):
            code, result, _ = self.cli('sync', '--shop-id', '1', '--no-sync')
        self.assertEqual(code, 1)
        self.assertEqual(result['data']['prefix_mapping']['mapped'], 0)
        self.assertEqual(result['data']['fetched'], [])
        self.assertEqual(document(1), previous)
        self.assertEqual(db.resolve_vc(old)['last_cn'], '旧名称')
        self.assertNotIn(new, db.vc_entries())

    def test_old_db_reads_are_byte_identical_and_next_save_writes_columns_and_envelope(self):
        snapshot(1, [], source='bcs', fetchedAt=OLD_STAMP)
        snapshot(2, [], source='wb-openapi', fetchedAt=OLD_STAMP)
        with db.connect(write=True) as connection:
            for name in db.SNAPSHOT_COLUMNS:
                connection.execute(f'ALTER TABLE shops DROP COLUMN {name}')
            connection.execute('ALTER TABLE products DROP COLUMN shipping_mode')
            connection.execute("""
                UPDATE shops SET snapshot_json =
                    json_remove(snapshot_json, '$.snapshotStatus', '$.lastConfirmedSource', '$.lastConfirmedSyncAt')
            """)
        database = Path(paths.current.database)
        original = database.read_bytes()
        for args in (('doctor',), ('shop', 'list'), ('catalog', 'product-summary')):
            with self.subTest(args=args):
                code, receipt, _ = self.cli(*args)
                self.assertEqual(code, 0)
                old_bcs, old_wb = receipt['data']['shops']
                self.assertEqual(old_bcs['snapshot_status'], 'legacy_unverified')
                self.assertEqual(old_bcs['last_fetched_at'], OLD_STAMP)
                self.assertIsNone(old_bcs['last_confirmed_sync_at'])
                self.assertEqual(old_wb['last_confirmed_sync_at'], OLD_STAMP)
                self.assertEqual(database.read_bytes(), original)
        with patch.object(self.client, 'fetch_shop_products', return_value=[]):
            code, receipt, _ = self.cli('catalog', 'fetch', '--shop-id', '1', '--no-sync')
        self.assertEqual(code, 0)
        self.assertEqual(set(receipt['data']), RECEIPT_KEYS)
        self.assertIn('已跳过同步', receipt['summary'])
        row = self.shop_row(1)
        envelope = json.loads(row['snapshot_json'])
        self.assertEqual(row['snapshot_status'], envelope['snapshotStatus'])
        self.assertEqual(row['snapshot_status'], 'bcs_cache_only')
        self.assertEqual(row['last_fetched_at'], envelope['fetchedAt'])
        self.assertIsNone(row['last_confirmed_sync_at'])
        self.assertIsNone(envelope['lastConfirmedSource'])
        self.assertIsNone(envelope['lastConfirmedSyncAt'])
        self.assertEqual(receipt['data']['fetched'][0]['fetchedAt'], row['last_fetched_at'])
        self.assertTrue(set(db.SNAPSHOT_COLUMNS) <= self.shop_row(2).keys())

    def test_cache_status_preserves_confirmation_and_never_resets_age(self):
        class Clock(datetime):
            instant = datetime(2026, 1, 1, 10)

            @classmethod
            def now(cls):
                return cls.instant

        with patch.object(products, 'datetime', Clock), patch('requests.request') as send:
            send.return_value = reply(code=200, rows=[], total=0)
            code, cache, _ = self.cli('sync', '--shop-id', '1', '--no-sync')
            self.assertEqual(code, 0)
            self.assertIsNone(cache['data']['fetched'][0]['last_confirmed_sync_at'])

            send.side_effect = [reply(code=200), reply(code=200, data={'status': 1}),
                                reply(code=200, rows=[], total=0)]
            code, confirmed, _ = self.cli('sync', '--shop-id', '1')
            self.assertEqual(code, 0)
            self.assertEqual(set(confirmed['data']), RECEIPT_KEYS)
            self.assertEqual(confirmed['summary'], '快照同步完成')
            known = confirmed['data']['fetched'][0]
            self.assertEqual(known['snapshot_status'], 'bcs_synced')
            self.assertEqual(known['last_confirmed_sync_at'], known['fetchedAt'])
            self.assertEqual(known['last_confirmed_source'], 'bcs')
            self.assertEqual(self.shop_row(1)['last_confirmed_sync_at'], known['fetchedAt'])
            self.assertEqual(document(1)['lastConfirmedSyncAt'], known['fetchedAt'])

            Clock.instant += timedelta(hours=1)
            write_book([('SKU', '缓存归属', 10, 'TEST')])
            send.side_effect = [reply(code=502), reply(code=200, rows=[{'vendorCode': 'BCS-TEST-1'}], total=1)]
            code, fallback, log = self.cli('catalog', 'fetch', '--shop-id', '1')
            self.assertEqual(code, 0)
            self.assertIn('同步未确认', fallback['summary'])
            self.assertEqual(fallback['data']['sync_warnings'][0]['stage'], 'submit')
            cached = fallback['data']['fetched'][0]
            self.assertEqual(cached['prefix_mapped'], 1)
            self.assertEqual(cached['snapshot_status'], 'bcs_cache_fallback')
            self.assertNotEqual(cached['fetchedAt'], known['fetchedAt'])
            self.assertEqual(cached['last_confirmed_sync_at'], known['fetchedAt'])
            self.assertEqual(document(1)['lastConfirmedSource'], 'bcs')
            self.assertNotIn('\r', log)
            with patch.object(shops, 'time', return_value=Clock.instant.timestamp()):
                freshness = self.cli('shop', 'list')[1]['data']['snapshot_freshness']
            self.assertEqual(freshness['shops'][0]['status'], 'cache_unconfirmed')

            Clock.instant += timedelta(hours=23)
            send.side_effect = None
            self.assertEqual(self.cli('sync', '--shop-id', '1', '--no-sync')[0], 0)
            with patch.object(shops, 'time', return_value=Clock.instant.timestamp()):
                freshness = self.cli('shop', 'list')[1]['data']['snapshot_freshness']
            self.assertEqual((freshness['shops'][0]['status'], freshness['shops'][0]['age_hours']), ('stale', 24))

            send.side_effect = [reply(code=200), reply(code=200, data={'status': 1, 'completedCount': 3}),
                                reply(code=200, rows=[], total=0)]
            self.assertEqual(self.cli('sync', '--shop-id', '1')[0], 0)
            with patch.object(shops, 'time', return_value=Clock.instant.timestamp()):
                self.assertEqual(self.cli('doctor')[1]['data']['snapshot_freshness']['shops'], [])

    def test_official_confirmation_is_preserved_after_reading_bcs_cache(self):
        with (patch.object(self.client, 'fetch_open_api_shops', return_value=[OpenAPIShop(1, 'Official', 'key')]),
              patch.object(products.snapshot_open_api, 'collect_shop', return_value=([], {})),
              patch.object(self.client, 'fetch_shop_products', return_value=[]) as cache):
            code, receipt, _ = self.cli('catalog', 'fetch', '--shop-id', '1', '--source', 'wb-openapi')
            self.assertEqual(code, 0)
            cache.assert_not_called()
            official = receipt['data']['fetched'][0]
            self.assertEqual(official['snapshot_status'], 'wb_openapi')
            code, receipt, _ = self.cli('sync', '--shop-id', '1', '--no-sync')
            self.assertEqual(code, 0)
            cached = receipt['data']['fetched'][0]
            self.assertEqual(cached['last_confirmed_source'], 'wb-openapi')
            self.assertEqual(cached['last_confirmed_sync_at'], official['fetchedAt'])

    def test_failure_registration_rollback_clear_and_concurrent_success(self):
        snapshot(1, [{'vendorCode': 'BEFORE'}], source='bcs', fetchedAt=OLD_STAMP)
        original = document(1)
        save = products.save_snapshot

        def fail_after_write(*args, **kwargs):
            save(*args, **kwargs)
            raise sqlite3.OperationalError('private disk detail')

        with (patch.object(self.client, 'fetch_shop_products', return_value=[{'vendorCode': 'AFTER'}]),
              patch.object(products, 'save_snapshot', side_effect=fail_after_write)):
            code, receipt, log = self.cli('sync', '--shop-id', '1', '--no-sync')
        self.assertEqual(code, 1)
        self.assertEqual(receipt['data']['fetched'], [])
        self.assertEqual(document(1), original)
        self.assertEqual(self.shop_row(1)['last_sync_error'], '本地快照保存失败')
        self.assertNotIn('private disk detail', log)

        def save_from_another_batch(*args, **kwargs):
            snapshot(1, [], source='bcs', fetchedAt='2026-01-02 10:00:00', snapshot_status='bcs_synced')
            raise NetworkError('private')

        with patch.object(self.client, 'fetch_shop_products', side_effect=save_from_another_batch):
            self.assertEqual(self.cli('sync', '--shop-id', '1', '--no-sync')[0], 1)
        self.assertIsNone(self.shop_row(1)['last_sync_error'])
        self.assertEqual(document(1)['fetchedAt'], '2026-01-02 10:00:00')
        with patch.object(self.client, 'fetch_shop_products', side_effect=NetworkError('private')):
            self.assertEqual(self.cli('sync', '--shop-id', '4', '--no-sync')[0], 1)
        self.assertIsNone(self.shop_row(4))

    def test_single_authentication_in_progress_never_reads_cache(self):
        snapshot(1, [], fetchedAt=OLD_STAMP)
        with patch('requests.request', side_effect=[reply(code=200), reply(code='403', msg='private')]) as send:
            code, receipt, log = self.cli('catalog', 'fetch', '--shop-id', '1')
        self.assertEqual(code, 1)
        data = receipt['data']
        self.assertEqual((data['sync_warnings'][0]['stage'], data['errors'][0]['kind']), ('poll', 'authentication'))
        self.assertEqual(data['fetched'], [])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(document(1)['fetchedAt'], OLD_STAMP)
        self.assertEqual(self.shop_row(1)['last_sync_error'], data['errors'][0]['error'])
        self.assertNotIn('private', log)

    def test_all_authentication_aborts_later_submissions_and_all_base_reads(self):
        with (patch.object(self.client, 'fetch_shop_list', return_value=STORES),
              patch('requests.request', return_value=reply(status=401)) as send):
            code, receipt, _ = self.cli('sync', '--all', '--bcs-parallel', '1')
        self.assertEqual(code, 1)
        data = receipt['data']
        self.assertEqual(data['fetched'], [])
        self.assertEqual([(w['id'], w['stage'], w['kind']) for w in data['sync_warnings']],
                         [(1, 'submit', 'authentication'), (2, 'not_started', 'authentication_aborted'),
                          (3, 'not_started', 'authentication_aborted')])
        self.assertEqual([e['kind'] for e in data['errors']],
                         ['authentication', 'authentication_aborted', 'authentication_aborted'])
        send.assert_called_once()
        self.assertIn('/product/sync?taskId=', send.call_args.args[1])

    def test_base_authentication_keeps_successful_shop_and_skips_remaining_base(self):
        snapshot(2, [], fetchedAt=OLD_STAMP)
        snapshot(3, [], fetchedAt=OLD_STAMP)
        with (patch.object(self.client, 'fetch_shop_list', return_value=STORES),
              patch.object(self.client, 'sync_shops_parallel', return_value=[]) as synchronize,
              patch('requests.request', side_effect=[reply(code=200, rows=[]), reply(code='401')]) as send):
            code, receipt, _ = self.cli('sync', '--all')
        self.assertEqual(code, 1)
        self.assertEqual(synchronize.call_args.kwargs['workers'], 2)
        data = receipt['data']
        self.assertEqual([row['id'] for row in data['fetched']], [1])
        self.assertEqual(data['fetched'][0]['snapshot_status'], 'bcs_synced')
        self.assertEqual([e['kind'] for e in data['errors']], ['authentication', 'authentication_aborted'])
        self.assertEqual(document(2)['fetchedAt'], OLD_STAMP)
        self.assertEqual(document(3)['fetchedAt'], OLD_STAMP)
        self.assertEqual(send.call_count, 2)

    def test_total_budget_includes_roster_and_allows_base_after_sync_deadline(self):
        clock = [0.0]

        def roster(*, budget):
            self.assertEqual(budget.deadline, 1800)
            clock[0] = 1500
            return STORES

        def fetch(sid, *, filter_type, budget):
            self.assertEqual((sid, filter_type, budget.remaining()), (1, 'BASE', 300))
            clock[0] = 1800
            return []

        with (patch.object(time, 'monotonic', side_effect=lambda: clock[0]),
              patch.object(self.client, 'fetch_shop_list', side_effect=roster),
              patch.object(self.client, 'fetch_shop_products', side_effect=fetch) as base,
              patch.object(self.client, 'sync_shop') as submit):
            code, receipt, _ = self.cli('sync', '--all', '--bcs-parallel', '4')
        self.assertEqual(code, 1)
        submit.assert_not_called()
        base.assert_called_once()
        data = receipt['data']
        self.assertEqual([(w['stage'], w['kind']) for w in data['sync_warnings']], [('not_started', 'batch_timeout')] * 3)
        self.assertEqual(data['fetched'][0]['snapshot_status'], 'bcs_cache_fallback')
        self.assertEqual([e['kind'] for e in data['errors']], ['batch_timeout', 'batch_timeout'])

    def test_both_parsers_validate_bcs_parallel_range(self):
        for args in (('sync',), ('catalog', 'fetch')):
            for limit in ('0', '5'):
                self.assertEqual(self.cli(*args, '--bcs-parallel', limit)[0], 2)


class TestBCSSyncProtocol(TestCase):
    def setUp(self):
        self.client = BCSClient(BCSSession('fixture', 'limit', 'Admin-Token=fixture; Limit-Key=limit'))

    def test_parallel_authentication_stops_inflight_polling_and_pending_shops(self):
        entered = Event()

        def submit(sid, *, budget):
            if sid == 2:
                if not entered.wait(3):
                    raise AssertionError('first shop failed to enter polling')
                raise AuthenticationError('private fixture')
            return str(sid)

        def poll(task, *, shop_id, quiet, budget):
            self.assertEqual((task, shop_id, quiet), ('1', 1, True))
            entered.set()
            budget.pause(3)

        log = io.StringIO()
        with (patch.object(self.client, 'sync_shop', side_effect=submit) as submitted,
              patch.object(self.client, 'wait_sync_done', side_effect=poll), redirect_stdout(log)):
            warnings = self.client.sync_shops_parallel([1, 2, 3, 4])
        self.assertEqual({c.args[0] for c in submitted.call_args_list}, {1, 2})
        self.assertEqual([(w['id'], w['stage'], w['kind']) for w in warnings],
                         [(1, 'poll', 'authentication_aborted'), (2, 'submit', 'authentication'),
                          (3, 'not_started', 'authentication_aborted'), (4, 'not_started', 'authentication_aborted')])
        self.assertNotIn('private fixture', log.getvalue())
        self.assertNotIn('\r', log.getvalue())

    def test_status_one_completes_without_full_progress_and_heartbeat_uses_newlines(self):
        for final in ({'status': 1}, {'status': 1, 'completedCount': 17}):
            with self.subTest(final=final), patch('time.sleep'), redirect_stdout(io.StringIO()) as log:
                with patch('requests.request', side_effect=[
                        reply(code=200, data={'status': 0, 'completedCount': 100}),
                        reply(code=200, data=final)]) as send:
                    self.client.wait_sync_done('fixture', shop_id=1)
                self.assertEqual(send.call_count, 2)
                self.assertEqual(log.getvalue().count('\n'), 1)
                self.assertNotIn('\r', log.getvalue())

    def test_polling_stops_at_earliest_deadline(self):
        for window, kind in ((4, 'batch_timeout'), (1800, 'shop_timeout')):
            clock = [0.0]

            def sleep(delay):
                clock[0] += delay

            with (self.subTest(window=window), patch.object(time, 'monotonic', side_effect=lambda: clock[0]),
                  patch.object(time, 'sleep', side_effect=sleep),
                  patch('requests.request', return_value=reply(code=200, data={'status': 0})) as send):
                with self.assertRaises(SyncInterrupted) as interrupted:
                    self.client.wait_sync_done('abc', timeout=999, interval=1000, quiet=True,
                                               budget=RequestBudget(window))
            send.assert_called_once()
            self.assertEqual(clock[0], min(window, 600))
            self.assertEqual(interrupted.exception.kind, kind)

    def test_read_backoff_can_exhaust_budget_and_submit_get_never_retries(self):
        clock = [0.0]

        def sleep(delay):
            clock[0] += delay

        with (patch.object(time, 'monotonic', side_effect=lambda: clock[0]),
              patch.object(time, 'sleep', side_effect=sleep),
              patch('requests.request', return_value=reply(status=429)) as send):
            with self.assertRaises(SyncInterrupted):
                self.client.fetch_shop_products(1, budget=RequestBudget(1))
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs['timeout'], (0.5, 0.5))
        self.assertIn('pageNum=1&pageSize=10000&shopId=1', send.call_args.args[1])
        self.assertEqual(clock[0], 1)
        with patch('requests.request', side_effect=requests.Timeout) as send:
            with self.assertRaises(NetworkError):
                self.client.sync_shop(1, budget=RequestBudget(time.monotonic() + 30))
        send.assert_called_once()
        self.assertFalse(send.call_args.kwargs['allow_redirects'])

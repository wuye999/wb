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

from catalog_fixture import document, mock_non_git_checkout, snapshot
from sellerops.cli.main import main
from sellerops.core.errors import AuthenticationError, NetworkError
from sellerops.features.catalog import products, shops
from sellerops.integrations.bcs.budget import RequestBudget, SyncInterrupted
from sellerops.integrations.bcs.client import BCSClient, OpenAPIShop
from sellerops.integrations.bcs.session import BCSSession
from sellerops.persistence import db
from sellerops.settings import paths
from test_bcs_auth import reply


RECEIPT_KEYS = {'source', 'fetchedAt', 'shops', 'fetched', 'sync_warnings', 'errors', 'missing_shops'}
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
            send.side_effect = [reply(code=502), reply(code=200, rows=[], total=0)]
            code, fallback, log = self.cli('catalog', 'fetch', '--shop-id', '1')
            self.assertEqual(code, 0)
            self.assertIn('同步未确认', fallback['summary'])
            self.assertEqual(fallback['data']['sync_warnings'][0]['stage'], 'submit')
            cached = fallback['data']['fetched'][0]
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

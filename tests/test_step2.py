"""WorkBuddy 入门命令和固定计划单的离线验收。"""
import io
import json
import csv
import tempfile
import unittest
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from catalog_fixture import document, map_vcs, mock_non_git_checkout, snapshot, write_book
from sellerops.cli.main import main
from sellerops.features.catalog import products
from sellerops.features.catalog.vc_mapping import multiplier_candidates
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
        mock_non_git_checkout(self)
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
        other = dict(row(C, 103, 1003), repImg='https://example.invalid/b.png')
        snapshot(1, [row(A, 101, 1001), row(B, 102, 1002), other], fetchedAt='2026-01-01')
        snapshot(2, [row(A, 201, 2001)], fetchedAt='2026-01-02')

    def confirm(self, *rows):
        return self.cli('map', 'batch-set', stdin=json.dumps(list(rows)))

    def test_review_price_empty_preview_has_no_plan_in_either_command(self):
        cred = Mock(root_version='fixture')
        cred.wb_shops.return_value = [{'shopId': 1, 'shopName': '测试店'}]
        with patch.object(wb.credentials, 'get', return_value=cred), patch.object(wb.time, 'sleep'), \
                patch.object(wb, 'make_session') as session, \
                patch.object(wb, 'request', return_value={'error': False, 'data': {'quarantineGoods': None}}) as request:
            for scope in ('ops', 'product'):
                rc, reply, _ = self.cli(scope, 'review-price', '--shops', '1')
                self.assertEqual((rc, reply['plan_id'], reply['summary']), (0, None, '所选店铺无待审核商品'))
                self.assertEqual(reply['data']['scope'], [])
                self.assertEqual(reply['data']['result']['failed_shops'], [])
        self.assertEqual([call.args[1] for call in request.call_args_list], ['GET', 'GET'])
        self.assertEqual(session.return_value.close.call_count, 2)

    def test_review_price_empty_explicit_shop_filter_does_not_query_all(self):
        cred = Mock(root_version='fixture')
        cred.wb_shops.return_value = [{'shopId': 1, 'shopName': '测试店'}]
        with patch.object(wb.credentials, 'get', return_value=cred), patch.object(wb, 'make_session') as session:
            rc, reply, _ = self.cli('ops', 'review-price', '--shops', ',')
        self.assertEqual((rc, reply['plan_id']), (2, None))
        session.assert_not_called()

    def test_review_price_partial_preview_freezes_only_the_shop_read_successfully(self):
        cred = Mock(root_version='fixture')
        cred.wb_shops.return_value = [{'shopId': sid, 'shopName': f'店{sid}'} for sid in (1, 2, 3)]
        sessions = {sid: Mock() for sid in (1, 2, 3)}
        review = {'id': 19, 'nmID': 101, 'vendorCode': A, 'oldPrice': 100, 'newPrice': 60}
        responses = [wb.common.CookieExpiredError('private-fixture'), {'error': False, 'data': {}},
                     {'error': False, 'data': {'quarantineGoods': [review]}}]
        with patch.object(wb.credentials, 'get', return_value=cred), patch.object(wb.time, 'sleep'), \
                patch.object(wb, 'make_session', side_effect=lambda shop, _: sessions[shop['shopId']]), \
                patch.object(wb, 'request', side_effect=responses) as request:
            rc, reply, stderr = self.cli('ops', 'review-price', '--all-shops')
        self.assertEqual(rc, 1)
        self.assertIsNotNone(reply['plan_id'])
        self.assertIn('部分店铺', reply['summary'])
        self.assertEqual([row['shopId'] for row in reply['data']['scope']], [3])
        self.assertEqual(reply['data']['scope'][0]['items'][0]['id'], 19)
        self.assertEqual([row['shop_id'] for row in reply['data']['result']['failed_shops']], [1, 2])
        self.assertNotIn('private-fixture', stderr + str(reply))
        self.assertEqual([call.args[1] for call in request.call_args_list], ['GET', 'GET', 'GET'])
        for session in sessions.values():
            session.close.assert_called_once()

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

    def test_doctor_reports_mabang_warehouse_configuration_without_values(self):
        self.populate()
        receipt = self.data / 'credentials.json'
        for warehouse, mode, valid in (
                ({}, 'missing', True),
                ({'warehouse_id': '42'}, 'id', True),
                ({'warehouse_keyword': '示例仓'}, 'keyword', True),
                ({'warehouse_keyword': '示例仓', 'warehouse_id': 42}, 'keyword_and_id', True),
                ({'warehouse_keyword': 5, 'warehouse_id': True}, 'invalid', False)):
            with self.subTest(mode=mode), patch.object(doctor, 'online_checks') as online:
                receipt.write_text(json.dumps({'mabang': warehouse}), encoding='utf8')
                _, reply, output = self.cli('doctor')
            data = reply['data']
            summary = data['platforms']['mabang']
            self.assertEqual(summary['warehouse_mode'], mode)
            self.assertEqual(summary['warehouse_configured'], mode not in ('missing', 'invalid'))
            self.assertEqual(data['credentials']['structure_valid'], valid)
            self.assertNotIn('示例仓', json.dumps(reply, ensure_ascii=False) + output)
            self.assertFalse(data['online_checked'])
            online.assert_not_called()
        self.assertEqual({i['code'] for i in data['credentials']['issues']},
                         {'expected_string', 'invalid_warehouse_id'})

    def test_price_preview_keeps_skip_csv_and_plan_in_json(self):
        write_book([('', '唯一中文名', 38, 'AAAA', '')])
        snapshot(1, [row(A, 101, 1001)], fetchedAt='2000-01-01T00:00:00+00:00',
                 source='bcs', snapshot_status='bcs_synced')
        snapshot(2, [])
        snapshot(3, [])
        rc, _, _ = self.confirm({'vc': A, 'action': 'map', 'cn': '唯一中文名'})
        self.assertEqual(rc, 0)
        rc, reply, err = self.cli('ops', 'price', '--shops', '1,2', '--name', '唯一中文名', '--price', '55')
        self.assertEqual(rc, 0, (reply, err))
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['plan_id'], 'P001')
        self.assertEqual(reply['data']['scope'][0]['items'][0]['cn'], '唯一中文名')
        freshness = reply['data']['snapshot_freshness']
        self.assertEqual([(row['shop_id'], row['status']) for row in freshness['shops']],
                         [('1', 'stale'), ('2', 'unknown_age')])
        self.assertIn('[快照提醒]', err)
        self.assertIn('apply --plan P001', reply['next_action'])
        skip_csv = reply['data']['scope'][0]['skip_report']
        self.assertTrue(Path(skip_csv).is_file())
        self.assertEqual(reply['data']['result']['artifacts']['skips_csv'], skip_csv)
        self.assertEqual(reply['data']['artifacts']['skips_csv'], skip_csv)
        with db.connect() as connection:
            plan = connection.execute('SELECT payload_json FROM plans WHERE plan_id=?', (reply['plan_id'],)).fetchone()
        frozen = json.loads(plan['payload_json'])
        self.assertEqual(frozen['scope'], reply['data']['scope'])
        self.assertNotIn('snapshot_freshness', frozen['arguments'])

    def test_large_stock_preview_keeps_full_csv_and_frozen_apply_scope(self):
        from sellerops.features.products import plans
        from sellerops.integrations.wildberries import stocks as wb_stocks

        specs = [{'chrtId': i, 'price': 38} for i in range(1100, 1200)]
        snapshot(1, [dict(row(A, 101, 1001), sizeList=specs)])
        options = ('ops', 'stock', '--shops', '1', '--vc', A, '--amount', '7')
        credentials = Mock(root_version='fixture')
        credentials.wb_shop.return_value = {'shopId': 1}
        with patch.object(plans.warehouse, 'resolve_warehouse', return_value=42), \
                patch.object(wb.credentials, 'get', return_value=credentials), \
                patch.object(wb, 'make_session', return_value=Mock()), \
                patch.object(wb_stocks, 'post_stocks', return_value={'error': False}) as send, \
                patch.object(wb.time, 'sleep'):
            code, compact, err = self.cli(*options)
            self.assertEqual(code, 0, (compact, err))
            data = compact['data']
            self.assertNotIn('scope', data)
            self.assertNotIn('receipts', data['result'])
            self.assertEqual(data['preview']['counts']['planned'], 100)
            self.assertEqual(data['preview']['omitted'], 90)
            self.assertEqual(data['preview']['per_shop'][0]['planned_cards'], 1)
            self.assertIn('snapshot_freshness', data)
            self.assertEqual(data['preview']['per_shop'][0]['planned_specs'], 100)
            self.assertEqual(len(data['preview']['samples']), 10)
            for key in ('receipts_csv', 'plan_csv'):
                with Path(data['artifacts'][key]).open(encoding='utf-8-sig', newline='') as file:
                    self.assertEqual(len(list(csv.reader(file))), 101)
            code, full, err = self.cli(*options, '--full-preview')
            self.assertEqual(code, 0, err)
            self.assertGreater(len(json.dumps(full)), 4 * len(json.dumps(compact)))
            with db.connect() as connection:
                stored = connection.execute('SELECT payload_json FROM plans WHERE plan_id=?', (compact['plan_id'],)).fetchone()
            self.assertEqual(json.loads(stored[0])['scope'], full['data']['scope'])
            self.assertNotIn('full_preview', json.loads(stored[0])['arguments'])
            send.assert_not_called()
            code, applied, err = self.cli('apply', '--plan', compact['plan_id'])
            self.assertEqual(code, 0, (applied, err))
            self.assertEqual(len(applied['data']['receipt']['receipts']), 100)
            self.assertEqual([item for call in send.call_args_list for item in call.args[2]],
                             [{'chrtId': spec['chrtId'], 'amount': 7} for spec in specs])

    def test_publish_preview_byte_limit_keeps_frozen_payload_out_of_summary(self):
        fields = {'nm_id': 101, 'vendor_code': A, 'price': 38, 'stock': 100,
                  'length': 1, 'width': 2, 'height': 3, 'weight': 0.5}
        payload = {'shop': json.dumps([{'id': 1, 'warehouseId': 42, 'warehouseQuantity': 0}]),
                   'media': 'fixture-' * 5000}
        frozen = {'items': [(fields, [1], payload)], 'warehouses': {1: 42}}
        args = Namespace(cmd='ops', action='create-cards', json=True)

        def prepare(options):
            options.prepared = frozen
            return 0

        with redirect_stdout(io.StringIO()):
            self.assertEqual(operation_plan.preview(args, prepare), 0)
        [example] = args.output['preview']['samples']
        self.assertEqual((example['vc'], example['warehouse_id'], example['targets']['stock']), (A, 42, 0))
        self.assertEqual(example['targets']['package'], {'length': 1, 'width': 2, 'height': 3, 'weight': 0.5})
        self.assertNotIn('scope', args.output)
        self.assertNotIn('fixture-', json.dumps(args.output))
        with Path(args.output['artifacts']['plan_csv']).open(encoding='utf-8-sig', newline='') as file:
            [entry] = list(csv.DictReader(file))
        self.assertEqual(json.loads(entry['目标值'])['stock'], 0)
        with db.connect() as connection:
            stored = connection.execute('SELECT payload_json FROM plans WHERE plan_id=?', (args.plan_id,)).fetchone()
        self.assertEqual(json.loads(stored[0])['scope']['items'][0][2], payload)

    def test_local_product_summary_uses_spec_original_prices_and_verified_stock(self):
        write_book([('S1', '旅行杯', 38), ('S2', '玻璃杯', 40)])
        timestamps = ['2026-01-01', '2026-01-02', '2026-01-03']
        first = dict(row(A, 101, 111), currencyIsoCode='CNY', discount=0, sizeList=[
            {'chrtId': 111, 'price': 40, 'discountedPrice': 10, 'stockList': [{'warehouseId': 11, 'amount': 2},
                                                                              {'warehouseId': 12, 'amount': 8}]},
            {'chrtId': 112, 'price': 55, 'stockList': [{'warehouseId': 11, 'amount': 3}]}])
        snapshot(1, [first, dict(row('TRASHED', 102, 113), trashedAt='date'), row('UNMAPPED', 103, 114),
                     row('EXCLUDED', 104, 115), row(C, 105, 116)], source='bcs', fetchedAt=timestamps[0])
        for sid, wid, quantities in ((2, 22, [0, 0]), (3, 33, [4, None])):
            sizes = [{'chrtId': i, 'price': price, 'discountedPrice': 1,
                      'stockList': [] if amount is None else [{'warehouseId': wid, 'amount': amount}]}
                     for i, price, amount in zip((211, 212), (60, 65), quantities)]
            snapshot(sid, [dict(row(A, sid * 100 + 1, 211), currencyIsoCode='CNY', discount=True, sizeList=sizes)],
                     source='wb-openapi', warehouse={'id': wid, 'name': '业务仓'}, fetchedAt=timestamps[sid - 1])
        snapshot(4, [row(A, 401, 411)])
        snapshot(5, [])
        map_vcs({A: 'S1', C: 'S2', 'TRASHED': 'S1'})
        with db.connect(write=True) as connection:
            db.set_vc(connection, 'EXCLUDED', 'excluded')
            connection.execute("UPDATE shops SET status='archived' WHERE shop_id='4'")
            connection.execute("INSERT INTO shops (shop_id, shop_name, status) VALUES ('6', '未同步', 'active')")
        (self.data / 'credentials.json').write_text('{"wb":{"warehouse_keyword":"业务仓"}}', encoding='utf8')
        database = Path(paths.current.database)
        before = database.read_bytes()
        rc, reply, err = self.cli('shop', 'list')
        self.assertEqual(rc, 0, err)
        stores = {s['shop_id']: s for s in reply['data']['shops']}
        self.assertEqual((stores['1']['active_item_count'], stores['1']['trashed_item_count']), (4, 1))
        self.assertEqual(stores['4']['active_item_count'], 1)
        self.assertEqual(stores['5']['active_item_count'], 0)
        self.assertIsNone(stores['6']['active_item_count'])
        rc, reply, err = self.cli('catalog', 'product-summary', '--sort', 'shops', '--limit', '1')
        self.assertEqual(rc, 0, (reply, err))
        data = reply['data']
        self.assertEqual(data['totals']['matched_groups'], 2)
        self.assertEqual(data['totals']['unmapped_items'], 1)
        [group] = data['groups']
        self.assertEqual((group['name'], group['item_count']), ('旅行杯', 3))
        self.assertEqual(group['original_price_by_currency'], [{'currency': 'CNY', 'min': 40, 'max': 65, 'known_specs': 6}])
        self.assertEqual(group['discount_pct'], {'min': 0, 'max': 0, 'known_items': 1, 'unknown_items': 2})
        self.assertEqual([entry['stock']['total_quantity'] for entry in group['per_shop']], [None, 0, None])
        self.assertEqual([s['warehouse']['id'] for s in data['shops']], [None, 22, 33, None, None])
        self.assertIn('来源 bcs | 采集 2026-01-01 | 确认同步 未知', err)
        rc, reply, _ = self.cli('catalog', 'product-summary', '--shops', '1', '--name-contains', '玻璃')
        self.assertEqual(rc, 0)
        self.assertEqual([group['name'] for group in reply['data']['groups']], ['玻璃杯'])
        self.assertEqual(self.cli('catalog', 'product-summary', '--shops', '4')[0], 1)
        self.assertEqual(database.read_bytes(), before)

    def test_replicate_zero_candidates_reports_target_reasons_with_csv(self):
        from sellerops.features.publishing import replicate

        snapshot(1, [row(A, 101, 111), row(C, 102, 112)])
        snapshot(2, [row(A, 201, 211)])
        snapshot(3, [dict(row(B, 301, 311), trashedAt='date')])
        snapshot(4, [])
        snapshot(5, [])
        with db.connect(write=True) as connection:
            db.set_vc(connection, A, 'pending', block_listing='5')
            db.set_vc(connection, C, 'excluded')
        with patch.object(replicate, 'load_records', return_value={A: {'4': 'date'}}), \
                patch.object(replicate, 'resolve_publish_details') as details:
            code, reply, err = self.cli('ops', 'replicate', '--source-shops', '1', '--shops', '2,3,4,5', '--all-products')
            details.assert_not_called()
        self.assertEqual(code, 0, (reply, err))
        self.assertIsNone(reply['plan_id'])
        summary = reply['data']['result']['replication']
        self.assertEqual((summary['candidate_vcs'], summary['fully_skipped_vcs'], summary['skipped_targets']), (0, 2, 8))
        self.assertEqual(summary['skip_reasons'], {'target_exact_vc': 1, 'target_same_source': 1, 'submitted': 1,
                                                   'listing_restricted': 1, 'excluded': 4})
        with Path(reply['data']['artifacts']['skips_csv']).open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 8)
        self.assertIn((B, 'trashed'), [(item['命中VC'], item['命中状态']) for item in rows])

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

    def test_candidate_group_and_batch_confirmation_are_atomic(self):
        self.populate()
        _, reply, _ = self.cli('map', 'candidates', '--limit', '1')
        [group] = reply['data']['groups']
        self.assertCountEqual(group['vcs'], [A, B])
        self.assertEqual((reply['data']['returned_groups'], reply['data']['returned_vcs']), (1, 2))
        item = next(i for i in group['items'] if i['vc'] == A)
        self.assertEqual(len(item['shops']), 2)
        self.assertEqual(item['prefix_candidate']['minimum_price'], 38)
        self.assertEqual((item['category'], item['nm_id']), ('prefix', 101))
        self.assertEqual(self.confirm({'vc': A, 'action': 'map', 'sku': 'S1'},
                                      {'vc': B, 'action': 'map', 'cn': '旅行杯'})[0], 1)
        self.assertEqual(db.resolve_vc(A)['status'], 'pending')
        self.assertEqual(self.confirm({'vc': A, 'action': 'map', 'sku': 'S1', 'source': 'prefix'},
                                      {'vc': C, 'action': 'block', 'shops': ['2']})[0], 0)
        self.assertEqual(db.resolve_vc(A)['source'], 'prefix')
        self.assertTrue(db.listing_blocked(C, 2))
        self.assertFalse(db.listing_blocked(C, 1))
        self.assertEqual(self.confirm({'vc': B, 'action': 'exclude'})[0], 0)
        self.assertEqual(db.resolve_vc(B)['status'], 'excluded')
        self.assertEqual(db.resolve_vc(C)['status'], 'pending')
        code, preview, _ = self.cli('map', 'batch-set', '--dry-run',
                                    stdin=json.dumps([{'vc': C, 'action': 'exclude'}]))
        self.assertEqual(code, 0)
        self.assertEqual(preview['data']['counts']['remaining_pending'], 0)
        self.assertIn('保存后用 map check-mismatch', preview['next_action'])
        self.assertEqual(db.resolve_vc(C)['status'], 'pending')

    def test_minimum_price_multiplier_candidates(self):
        product = {'sku': 'CAT', 'cn': '猫碗', 'minimum_price': 60}
        for price, multiplier, diff in [(60, 1, 0), (120, 2, 0), (600, 10, 0), (420, 7, 0),
                                        (59.4, 1, .6), (61, 1, 1), (122.4, 2, 2.4)]:
            with self.subTest(price=price):
                self.assertEqual(multiplier_candidates(price, [product]), [
                    {**product, 'multiplier': multiplier, 'expected_price': 60 * multiplier, 'diff': diff}
                ])
        for price in (None, 0, -60, 20, 75, 122.41):
            with self.subTest(price=price):
                self.assertEqual(multiplier_candidates(price, [product]), [])
        self.assertEqual(multiplier_candidates(60, [{**product, 'minimum_price': v} for v in (None, 0, -5)]), [])
        products = [{**product, 'sku': sku, 'minimum_price': price}
                    for sku, price in [('C', 60), ('B', 60), ('A', 60), ('D', 30), ('E', 20), ('F', 12)]]
        self.assertEqual([item['sku'] for item in multiplier_candidates(60, products)], ['A', 'B', 'C', 'D', 'E'])
        self.assertEqual([item['sku'] for item in multiplier_candidates(60, [
            {**product, 'sku': 'LOW', 'minimum_price': 59},
            {**product, 'sku': 'HIGH', 'minimum_price': 61}
        ])], ['HIGH', 'LOW'])

    def test_workbench_groups_source_and_image_and_filters_snapshot_scope(self):
        img = 'https://basket-01.wbbasket.ru/vol1/part123/123456/images/tm/1.webp'
        same_image = 'BCS-DIFF-x/234567-1'
        manual = 'BCS-DIFF-x/345678-1'
        archived = 'BCS-ARCH-x/456789-1'
        trashed = 'BCS-TRSH-x/567890-1'
        write_book([('CAT', '猫碗', 60, 'AAAA', ''), ('CUP', '水杯', 90, 'BBBB', '')])
        prefix = dict(row(A, 101, 1001), repImg='', title='Кошачья миска <test>')
        bridge = dict(row(B, 102, 1002), repImg=img)
        shared = dict(row(same_image, 103, 1003), repImg=img, title='Другое название')
        multiplier = dict(row(C, 123456, 1004), repImg='', sizeList=[{'chrtId': 1004, 'price': 120}])
        unmatched = dict(row(manual, 0, 1005), repImg='')
        snapshot(1, [prefix, bridge, shared, multiplier, unmatched,
                     dict(row(trashed, 105, 1006), trashedAt='2026-01-01')])
        snapshot(2, [prefix])
        snapshot(3, [row(archived, 106, 1007)])
        self.assertEqual(self.cli('shop', 'archive', '--shop-id', '3')[0], 0)

        _, reply, _ = self.cli('map', 'candidates')
        first, second, third = reply['data']['groups']
        self.assertCountEqual(first['vcs'], [A, B, same_image])
        self.assertEqual([group['category'] for group in (first, second, third)], ['prefix', 'multiplier', 'unmatched'])
        self.assertEqual(second['items'][0]['price_candidates'][0]['multiplier'], 2)
        self.assertEqual(third['items'][0]['price_candidates'], [])

        code, reply, err = self.cli('map', 'workbench')
        self.assertEqual(code, 0, err)
        self.assertEqual(reply['data']['counts'], {'groups': 3, 'vcs': 5, 'prefix': 1, 'multiplier': 1, 'unmatched': 1})
        _, limited, _ = self.cli('map', 'candidates', '--limit', '1')
        self.assertEqual(limited['data']['counts'], reply['data']['counts'])
        self.assertEqual(limited['data']['returned_vcs'], 3)
        self.assertTrue(limited['data']['truncated'])
        path = Path(reply['data']['artifacts']['html'])
        self.assertEqual(path.parent, self.root / 'exports' / 'workbench')
        html = path.read_text(encoding='utf8')
        for text in ('前缀命中', '倍数参考', '待手动归属', '3 个 VC', '猫碗 · 最低价¥60 ×2',
                     'width:150px;height:150px', 'list="products"', '<dialog id="lightbox"',
                     'localStorage.setItem', '导出审核结果 JSON', '已处理', '未处理',
                     '<progress id="progress"', '已排除 · 非货盘', '已归属 · ', '当前被筛选隐藏的组',
                     '请先归属或排除至少一组商品', '发给 WorkBuddy 的话术', 'map batch-set --file',
                     img.replace('/tm/', '/c246x328/'), img.replace('/tm/', '/big/'), img,
                     f'{wb.basket_base(123456)}/images/c246x328/1.webp'):
            self.assertIn(text, html)
        self.assertNotIn(archived, html)
        self.assertNotIn(trashed, html)
        self.assertEqual(self.cli('catalog', 'mapping')[1]['data'], reply['data'])
        self.assertEqual(path.read_text(encoding='utf8'), html)

        review = self.root / '待认领审核.json'
        review.write_text(json.dumps([
            *({'vc': vc, 'action': 'map', 'sku': 'CAT', 'cn': '猫碗'} for vc in first['vcs']),
            {'vc': manual, 'action': 'exclude'}
        ], ensure_ascii=False), encoding='utf-8')
        code, result, err = self.cli('map', 'batch-set', '--file', str(review))
        self.assertEqual(code, 0, err)
        self.assertCountEqual(result['data']['updated_vcs'], [A, B, same_image, manual])
        code, result, err = self.cli('catalog', 'merge', str(review))
        self.assertEqual(code, 0, err)
        self.assertCountEqual(result['data']['updated_vcs'], [A, B, same_image, manual])
        self.assertEqual({db.resolve_vc(vc)['sku'] for vc in first['vcs']}, {'CAT'})
        self.assertEqual(db.resolve_vc(manual)['status'], 'excluded')
        self.assertEqual(db.resolve_vc(C)['status'], 'pending')

    def test_group_with_conflicting_prefixes_has_no_bulk_suggestion(self):
        other = 'BCS-BBBB-source/123456-9'
        write_book([('CAT', '猫碗', 60, 'AAAA', ''), ('CUP', '水杯', 90, 'BBBB', '')])
        snapshot(1, [row(A, 101, 1001), row(other, 102, 1002)])
        _, reply, _ = self.cli('map', 'candidates')
        [group] = reply['data']['groups']
        self.assertEqual([item['category'] for item in group['items']], ['prefix', 'prefix'])
        self.assertIsNone(group['prefix_candidate'])
        self.assertEqual(group['category'], 'unmatched')

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

    def test_price_requires_an_explicit_target_or_keep_price_with_discount(self):
        for command in ('product', 'ops'):
            for options in ((), ('--discount', '0'), ('--keep-price',),
                            ('--channel', 'bcs', '--club-discount', '10')):
                with self.subTest(command=command, options=options):
                    arguments = (command, 'price', '--shops', '1', '--vc', A, *options)
                    if command == 'ops':
                        code, reply, _ = self.cli(*arguments)
                        self.assertIsNone(reply['plan_id'])
                        message = reply['summary']
                    else:
                        with redirect_stderr(io.StringIO()) as errors:
                            code = main(['--data-dir', str(self.data), *arguments])
                        message = errors.getvalue()
                    self.assertEqual(code, 1)
                    self.assertIn('必须使用 --price 指定目标价', message)
                    self.assertIn('--keep-price', message)
        self.assertFalse(self.data.exists())

    def test_bulk_publish_apply_uses_frozen_warehouse_price_and_task_receipt(self):
        from sellerops.domain.models import PublishItem
        from sellerops.features.products import warehouse
        from sellerops.features.publishing import bulk_publish, publish_details

        self.populate()
        item = PublishItem(101, price=59, vendor_code='BCS-AAAA-101', prefix='BCS-AAAA',
                           length=10, width=20, height=30, weight=.2, stock=0, target_shops=[1],
                           extra={'seller_sku': 'S1', 'vc_source': 'registered_prefix'})
        with patch.object(publish_details, 'resolve_publish_details', return_value=(item, None)), \
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

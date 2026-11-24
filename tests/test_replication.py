"""Copying must preserve identity, exact pricing, scope and pre-write preparation."""
import contextlib
from dataclasses import asdict
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from sellerops.domain.models import PublishItem
from sellerops.domain.vendor_codes import extract_vc_source_code
from sellerops.features.publishing import bulk_publish, card_publish, publish_details, publish_records, replicate
from sellerops.features.publishing.card_payload import build_card_payload
from sellerops.persistence import db, open_api_publish as store
from sellerops.persistence.snapshots import ProductSnapshotRepository as snapshots


def item(vc='BCS-TEST-1', targets=None):
    return PublishItem(nm_id=777, vendor_code=vc, price=415.25, cn='fixture',
                       length=10, width=20, height=30, weight=0.5, stock=0,
                       target_shops=[42] if targets is None else targets,
                       extra={'card_info': {'imt_name':'fixture','data':{'subject_id':1}},
                              'wb_detail': {'subjectId':1}})


class TestReplication(TestCase):
    def setUp(self):
        from sellerops.settings import paths

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(folder.name)

    def test_extracts_first_longest_digit_segment_with_optional_minimum(self):
        self.assertEqual(extract_vc_source_code('BCS-TEST-11/000123-654321'), '000123')
        self.assertEqual(extract_vc_source_code('BCS-TEST-123456', min_digits=6), '123456')
        self.assertIsNone(extract_vc_source_code('BCS-TEST-12345', min_digits=6))
        self.assertIsNone(extract_vc_source_code('BCS-TEST'))
        self.assertEqual(db.source_code('BCS-TEST-1-2'), '1')

    def test_source_union_deduplicates_full_vc_without_using_target_as_source(self):
        row = {'vendorCode':'BCS-TEST-1','nmId':999,'sizeList':[{'price':1}]}
        shops = {1:[row],2:[{**row,'nmId':888}],42:[],43:[{'vendorCode':'target-only'}]}
        state = {'BCS-TEST-1':{'cn':'fixture','nmId':777}}
        plans, blocked = replicate.build_replication_items(shops,state,
            [{'cn':'fixture','minimum_price':41.525}],source_shops=[1,2],target_shops=[42,43],
            price_source='minimum',price_multiplier=10,stock=999)
        self.assertEqual(blocked,[])
        self.assertEqual(len(plans),1)
        self.assertEqual((plans[0].vendor_code, plans[0].nm_id, plans[0].price, plans[0].stock),
                         ('BCS-TEST-1',999,415.25,999))
        self.assertEqual(plans[0].target_shops,[42,43])

    def test_ordinary_publish_resolves_source_price_and_current_main_book_identity(self):
        from catalog_fixture import snapshot, map_vcs, write_book
        from sellerops.persistence.mappings import MappingRepository

        vc = 'BCS-TEST-1'
        write_book([('S1', 'fixture title', 60, 'TEST', '')])
        snapshot(41, [{'vendorCode': vc, 'nmId': 777,
                       'sizeList': [{'chrtId': 987, 'price': 52}]}])
        map_vcs({vc: 'S1'})
        candidate = item()
        candidate.price, candidate.cn = None, ''
        state, _ = MappingRepository.load_mapping_state()
        resolved, error = publish_details.resolve_publish_details(candidate, state)
        self.assertIsNone(error)
        self.assertEqual((resolved.price, resolved.cn, resolved.nm_id), (52, 'fixture title', 777))

    def test_ordinary_publish_requires_a_price_when_source_snapshot_has_none(self):
        from catalog_fixture import write_book

        write_book([('S1', 'fixture', 60, 'TEST', '')])
        candidate = item()
        candidate.price = None
        resolved, error = publish_details.resolve_publish_details(candidate)
        self.assertIsNone(resolved)
        self.assertIn('--price', error)

    def test_bcs_marks_submitting_before_send_and_open_api_respects_unknown(self):
        from sellerops.features.publishing import publish_records
        from sellerops.persistence import db, open_api_publish

        candidate = item()
        def fail():
            self.assertEqual({row['status'] for row in db.plans('publish', 'legacy-publish:')},
                             {'submitting'})
            raise TimeoutError('fixture')

        send = Mock(side_effect=fail)
        with self.assertRaises(TimeoutError):
            publish_records.submit_once([candidate], [42], send)
        self.assertTrue(open_api_publish.legacy_submitted(42, candidate.vendor_code))
        self.assertEqual({row['status'] for row in db.plans('publish', 'legacy-publish:')}, {'unknown'})
        with self.assertRaises(ValueError):
            publish_records.submit_once([candidate], [42], send)
        send.assert_called_once()

    def test_missing_snapshot_fails_and_snapshot_trash_is_not_a_source(self):
        with self.assertRaisesRegex(ValueError,'缺少本地快照'):
            replicate.build_replication_items({1:[]},{},[],source_shops=[1],target_shops=[42])
        plans, _ = replicate.build_replication_items({1:[{'vendorCode':'x','trashedAt':'date'}],42:[]},
                                                     {},[],source_shops=[1],target_shops=[42])
        self.assertEqual(plans,[])

    def test_replication_gaps_exclude_same_source_live_trash_and_record_per_shop(self):
        source = {'vendorCode': 'BCS-SRCX-123456-1', 'nmId': 777, 'sizeList': [{'price': 1}]}
        shops = {1: [source], 42: [{'vendorCode': 'BCS-LIVE-123456'}],
                 43: [{'vendorCode': 'BCS-BINX-123456-2', 'trashedAt': 'date'}], 44: [], 45: []}
        plans, blocked = replicate.build_replication_items(
            shops, {}, [], source_shops=[1], target_shops=[42, 43, 44, 45],
            records={'BCS-PAST-123456': {'44': 'today'}},
        )
        self.assertEqual(blocked, [])
        self.assertEqual([(it.vendor_code, it.target_shops) for it in plans], [(source['vendorCode'], [45])])

    def test_ambiguous_minimum_and_nonfinite_multiplier_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'不一致'):
            replicate.replication_price({},'fixture',[{'cn':'fixture','minimum_price':1},
                {'cn':'fixture','minimum_price':2}],'minimum',10)
        for factor in [0,-1,float('inf'),float('nan')]:
            with self.subTest(factor=factor), self.assertRaises(ValueError):
                replicate.replication_price({},'fixture',[{'cn':'fixture','minimum_price':1}],
                                             'minimum',factor)

    def test_payload_preserves_vc_decimal_price_zero_stock_and_new_card_semantics(self):
        payload, err = build_card_payload(item(), [42, 43], {42: 100, 43: 101}, item().extra['card_info'])
        self.assertIsNone(err)
        self.assertEqual(json.loads(payload['shop']), [
            {'id': 42, 'warehouseId': 100, 'warehouseQuantity': 0},
            {'id': 43, 'warehouseId': 101, 'warehouseQuantity': 0},
        ])
        self.assertEqual(payload['mergeCards'], 1)
        self.assertEqual(len(payload['shopDatas']), 1)
        card = payload['shopDatas'][0]
        self.assertEqual(card['vendorCode'],'BCS-TEST-1')
        self.assertIsNone(card['nmId'])
        self.assertEqual(json.loads(card['wbDetail'])['sizes'][0]['price'],'415.25')
        self.assertEqual(payload['offerUrl'], 'https://www.wildberries.ru/catalog/777/detail.aspx')
        self.assertEqual(card['offerUrl'], payload['offerUrl'])
        self.assertEqual(card['videos'], card['video'])
        self.assertEqual(card['videos'], '')
        self.assertIsNone(payload['vendorCodePrefix'])
        self.assertIs(payload['removeBrandText'], False)

    def test_invalid_payload_is_blocked_before_submission(self):
        for values in [{'price':float('nan')},{'stock':-1},{'weight':0},{'vendor_code':''}]:
            it = item()
            for key,value in values.items():
                setattr(it,key,value)
            with self.subTest(values=values):
                payload, err = build_card_payload(it,[42],{42:100},it.extra['card_info'])
                self.assertIsNone(payload)
                self.assertTrue(err)

    def test_resolver_uses_supplied_metadata_and_preserves_explicit_zero(self):
        with patch.object(publish_details,'fetch_card_json',side_effect=AssertionError('unexpected read')):
            resolved, err = publish_details.resolve_publish_details(
                item(), mapping_state={}, price_book=[{'sku': 'S1', 'cn': 'fixture', 'prefix': 'TEST'}])
        self.assertIsNone(err)
        self.assertEqual(resolved.stock,0)
        self.assertEqual(resolved.price,415.25)

    def test_minimum_formula_and_package_columns_follow_headers(self):
        import openpyxl
        from sellerops.persistence.mappings import MappingRepository
        from sellerops.features.publishing import product_details
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'book.xlsx'
            book = openpyxl.Workbook()
            sheet = book.active
            sheet.title = 'Sheet1'
            sheet.append(['尺寸','最低售价','产品中文名','卖家SKU'])
            sheet.append(['10*20*30/0.5','=83.05/2','fixture','sku'])
            book.save(file)
            book.close()
            with patch.object(replicate.config.current,'price_book_xlsx',str(file)), \
                    patch.object(product_details.config, 'current', product_details.config.current):
                rows = MappingRepository.load_price_book()
                self.assertEqual(rows[0]['minimum_price'],41.525)
                self.assertEqual(product_details.price_book_packages(product_details.config.current)['fixture'],(10,20,30,0.5))

    def test_all_reads_finish_before_first_write_and_only_target_warehouse_is_read(self):
        events = []
        one, two = item(), item('BCS-TEST-2')
        two.nm_id = 778
        one.extra = {}
        two.extra = {'wb_detail': {'subjectId': 1, 'sizes': [
            {'origName': 42, 'name': 'M', 'price': '10.00', 'vendorCode': 'SOURCE-VC'},
            {'origName': 44, 'name': 'L'},
        ]}}
        card_info = {'imt_name': 'fixture', 'description': 'source description',
                     'data': {'subject_id': 1}}

        def read(nm):
            events.append('read')
            return card_info

        def submit(url, payload):
            events.append('write')
            return {'code':200}

        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(card_publish,'fetch_card_json',side_effect=read), \
                patch.object(card_publish,'resolve_warehouse',return_value=100) as wh, \
                patch.object(card_publish.bcs,'fetch_shop_list',side_effect=AssertionError('unrelated shop read')), \
                patch.object(card_publish,'load_records',return_value={}), \
                patch.object(card_publish, 'bcs', base_url='https://fixture.invalid') as client:
            client.post.side_effect = submit
            result = card_publish.execute_publish([one,two],apply=True,interval=0,writer=Mock(),check_exists=False)
        wh.assert_called_once_with(42)
        client.get.assert_not_called()
        self.assertEqual(events,['read','read','write','write'])
        self.assertEqual((result['ok'],result['fail']),(2,0))
        for call, it, orig_name, name in zip(client.post.call_args_list, [one, two], [0, 42], ['', 'M']):
            url, payload = call.args
            self.assertEqual(url, 'https://fixture.invalid/system/wbCollection/wb/new')
            card = payload['shopDatas'][0]
            self.assertEqual(json.loads(card['wbCardInfo']), card_info)
            self.assertEqual(json.loads(card['wbDetail'])['sizes'], [
                {'origName': orig_name, 'name': name, 'price': '415.25', 'vendorCode': it.vendor_code},
            ])

    def test_one_missing_target_warehouse_blocks_group_instead_of_silent_partial_success(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(card_publish,'load_records',return_value={}), \
                patch.object(card_publish.bcs,'post') as post:
            result = card_publish.execute_publish([item(targets=[42,43])],apply=True,writer=Mock(),
                                                 check_exists=False,warehouses={42:100})
        self.assertEqual(result['fail'],1)
        post.assert_not_called()

    def test_repeated_vc_target_in_input_is_only_submitted_once(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(card_publish,'load_records',return_value={}), \
                patch.object(card_publish.bcs,'post',return_value={'code':200}) as post:
            result = card_publish.execute_publish([item(),item()],apply=True,writer=Mock(),
                                                 interval=0,check_exists=False,warehouses={42:100})
        self.assertEqual((result['ok'],result['skip']),(1,1))
        post.assert_called_once()

    def test_default_duplicate_check_reads_local_snapshot_and_never_remote_catalog(self):
        from sellerops.persistence.snapshots import ProductSnapshotRepository as snapshots
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(snapshots,'exists',return_value=True), \
                patch.object(snapshots,'load_shop_products',return_value=[{'vendorCode':'BCS-TEST-1','trashedAt':'date'}]), \
                patch.object(card_publish,'load_records',return_value={}), \
                patch.object(card_publish.bcs,'get',side_effect=AssertionError('remote catalog read')), \
                patch.object(card_publish.bcs,'post') as post:
            result = card_publish.execute_publish([item()],apply=True,writer=Mock(),warehouses={42:100})
        self.assertEqual((result['ok'],result['skip'],result['fail']),(0,1,0))
        post.assert_not_called()

    def test_missing_target_snapshot_is_not_interpreted_as_empty_shop(self):
        from sellerops.persistence.snapshots import ProductSnapshotRepository as snapshots
        with patch.object(snapshots,'exists',return_value=False), \
                patch.object(card_publish,'load_records',return_value={}), \
                patch.object(card_publish.bcs,'post') as post, \
                self.assertRaisesRegex(ValueError,'缺少本地快照'):
            card_publish.execute_publish([item()],apply=True,writer=Mock(),warehouses={42:100})
        post.assert_not_called()

    def test_card_checks_each_shop_snapshot_and_shared_records_by_same_source(self):
        snapshots.save_shop_products(42, [{'vendorCode': 'BCS-BINX-123456', 'nmId': 420, 'trashedAt': 'date'}])
        snapshots.save_shop_products(43, [])
        snapshots.save_shop_products(44, [{'vendorCode': 'BCS-LIVE-1-2', 'nmId': 440}])
        store.record_shared(43, ['BCS-PAST-123456'])
        candidate = item('BCS-TEST-123456-1', [42, 43, 44])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(card_publish.bcs, 'post', return_value={'code': 200}) as post:
            result = card_publish.execute_publish([candidate], apply=True, writer=Mock(),
                                                  warehouses={42: 100, 43: 101, 44: 102})
        self.assertEqual(result['details'][0]['targets'], [44])
        post.assert_called_once()
        for vc in ['BCS-BINX-123456', 'BCS-PAST-123456']:
            self.assertIn(f'命中目标店已有同源商品 VC：{vc}', output.getvalue())

    def test_confirmed_card_plan_rechecks_same_source_without_changing_remaining_shop_payload(self):
        warehouses = {42: 100, 43: 101, 44: 102}
        for sid in warehouses:
            snapshots.save_shop_products(sid, [])
        candidate = item('BCS-TEST-123456', list(warehouses))
        with contextlib.redirect_stdout(io.StringIO()), patch.object(card_publish.bcs, 'post', return_value={'code': 200}) as post:
            preview = card_publish.execute_publish([candidate], warehouses=warehouses, capture=True)
            post.assert_not_called()
            snapshots.save_shop_products(42, [{'vendorCode': 'BCS-BINX-123456', 'nmId': 420, 'trashedAt': 'date'}])
            store.record_shared(43, ['BCS-PAST-123456'])
            result = card_publish.execute_publish([], apply=True, writer=Mock(), frozen=preview['prepared'])
        self.assertEqual(result['details'][0]['targets'], [44])
        post.assert_called_once()
        self.assertEqual(json.loads(post.call_args.args[1]['shop']), [
            {'id': 44, 'warehouseId': 102, 'warehouseQuantity': 0},
        ])

    def test_bulk_checks_local_snapshot_and_records_even_without_remote_duplicate_check(self):
        snapshots.save_shop_products(42, [{'vendorCode': 'BCS-LIVE-123456', 'nmId': 420}])
        store.record_shared(43, ['BCS-PAST-123456-1'])
        candidate = item('BCS-TEST-123456', [42, 43, 44])
        candidate.nm_id, candidate.prefix = 123456, 'BCS-TEST'
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(bulk_publish.bcs, 'fetch_shop_list', return_value=[{'id': sid} for sid in (42, 43, 44)]), \
                patch.object(bulk_publish, 'resolve_warehouse', side_effect=lambda sid: sid + 100), \
                patch.object(bulk_publish, 'vc_exists_in_shop') as remote_check, \
                patch.object(bulk_publish.bcs, 'batch_push_products', return_value={'code': 200}) as push:
            result = bulk_publish.execute_publish([candidate], apply=True, writer=Mock(), check_exists=False)
        self.assertEqual(result['details'][0]['targets'], [44])
        self.assertEqual([shop['id'] for shop in push.call_args.args[0]], [44])
        remote_check.assert_not_called()

    def test_simple_publish_needs_no_price_book_and_limits_after_duplicate_filter(self):
        from sellerops.persistence.mappings import MappingRepository

        skipped = item('BCS-TEST-123456')
        first = asdict(item('BCS-TEST-1-2'))
        first['card_info'] = first.pop('extra')['card_info']
        del first['stock']
        second = item('BCS-OTHR-1-2')
        second.nm_id = 778
        snapshots.save_shop_products(42, [{'vendorCode': 'BCS-BINX-123456', 'nmId': 420},
                                          {'vendorCode': 'BCS-LIVE-1-2', 'nmId': 421}])
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                patch.object(MappingRepository, 'load_price_book', side_effect=AssertionError('unexpected price book read')), \
                patch.object(card_publish, 'resolve_warehouse', return_value=100), \
                patch.object(card_publish.bcs, 'post', return_value={'code': 200}) as post:
            preview = card_publish.publish_cards_simple([skipped, first, second], limit=1)
            self.assertEqual((preview['ok'], preview['skip'], preview['fail']), (0, 1, 0))
            self.assertIn(first['vendor_code'], output.getvalue())
            self.assertNotIn(second.vendor_code, output.getvalue())
            post.assert_not_called()
            result = card_publish.publish_cards_simple([skipped, first, second], apply=True, limit=1)
        self.assertEqual((result['ok'], result['skip'], result['fail']), (1, 1, 0))
        post.assert_called_once()
        payload = post.call_args.args[1]
        self.assertEqual(payload['shopDatas'][0]['vendorCode'], first['vendor_code'])
        self.assertEqual(json.loads(payload['shop'])[0]['warehouseQuantity'], 999)

    def test_today_count_uses_actual_vcs_and_deduplicates_across_receipts(self):
        day, stamp = '2026-11-24', '2026-11-24 10:00:00'
        candidate = item('REQUESTED-123456')
        candidate.extra['k'] = 'another-key-654321'
        with patch.object(db, 'now', return_value=stamp):
            def send():
                self.assertEqual(publish_records.today_publish_counts(), {42: 1})
                return {'code': 200}

            publish_records.submit_once([candidate], [42], send, actual_vcs=('123456',))
        with db.connect(write=True) as connection:
            store.write_shared(connection, {'BCS-DRAFT-123456': {'42': stamp}}, status='draft')
            store.write_shared(connection, {'876543': {'42': stamp}, 'BCS-OLDX-123458': {'42': stamp}})
            store.write_shared(connection, {'BCS-UNKN-123457': {'43': stamp}}, status='unknown')
            store.write_shared(connection, {'BCS-YEST-123456': {'42': '2026-11-23 10:00:00'}})
            key = store.item_key(42, '123456')
            db.save_plan(connection, f'openapi:{key}', 'publish', 'partial', key, {'vc': '123456'}, stamp)
            db.save_plan(connection, 'P001', 'publish', 'submitting', 'P001', {}, stamp)
        self.assertEqual(publish_records.today_publish_counts(day), {42: 2, 43: 1})

    def test_daily_limit_warns_only_over_1000_in_preview_and_apply(self):
        expected = [{'shop_id': 42, 'today_submitted': 1000, 'pending': 1, 'total': 1001}]
        for apply in (False, True):
            with self.subTest(apply=apply), contextlib.redirect_stdout(io.StringIO()) as output, \
                    patch.object(card_publish, 'today_publish_counts', return_value={42: 1000, 43: 999}), \
                    patch.object(card_publish.bcs, 'post', return_value={'code': 200}) as post:
                result = card_publish.execute_publish([item(targets=[42, 43])], apply=apply, writer=Mock(),
                                                      warehouses={42: 100, 43: 101}, check_exists=False)
                self.assertEqual(result['daily_limit_warnings'], expected)
                self.assertIn('[警告] 店铺 42 今日已提交 1000 张，本次待提交 1 张，合计 1001 张', output.getvalue())
                self.assertEqual(post.call_count, int(apply))
                if apply:
                    self.assertEqual(result['details'][0]['targets'], [42, 43])

"""Copying must preserve identity, exact pricing, scope and pre-write preparation."""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from sellerops.domain.models import PublishItem
from sellerops.features.publishing import card_publish, publish_details, replicate
from sellerops.features.publishing.card_payload import build_card_payload


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

    def test_ambiguous_minimum_and_nonfinite_multiplier_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'不一致'):
            replicate.replication_price({},'fixture',[{'cn':'fixture','minimum_price':1},
                {'cn':'fixture','minimum_price':2}],'minimum',10)
        for factor in [0,-1,float('inf'),float('nan')]:
            with self.subTest(factor=factor), self.assertRaises(ValueError):
                replicate.replication_price({},'fixture',[{'cn':'fixture','minimum_price':1}],
                                             'minimum',factor)

    def test_payload_preserves_vc_decimal_price_zero_stock_and_new_card_semantics(self):
        payload, err = build_card_payload(item(),[42],{42:100},item().extra['card_info'])
        self.assertIsNone(err)
        self.assertEqual(json.loads(payload['shop'])[0],{'id':42,'warehouseId':100,'warehouseQuantity':0})
        card = payload['shopDatas'][0]
        self.assertEqual(card['vendorCode'],'BCS-TEST-1')
        self.assertIsNone(card['nmId'])
        self.assertEqual(json.loads(card['wbDetail'])['sizes'][0]['price'],'415.25')

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

"""合成夹具验证官方商品写入；不读取真实凭证、不连接平台。"""
import copy
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from sellerops.core.errors import AuthenticationError, NetworkError, PlatformApiError
from sellerops.core.files import atomic_dump_json
from sellerops.features.listings import open_api_payloads as payloads
from sellerops.features.listings import open_api_publish as publish
from sellerops.features.listings import open_api_finish as finish
from sellerops.features.listings import open_api_sync as sync
from sellerops.features.listings import open_api_sources as sources
from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.integrations.wildberries.content_api import WBContentAPI
from sellerops.integrations.wildberries.open_api import WBOpenAPI
from sellerops.persistence import open_api_publish as store
from sellerops.settings import paths


VC = 'BCS-TEST-999'
SCHEMA = [{'charcID': 10, 'name': 'Цвет', 'required': True, 'charcType': 1}]


def card(nm=101, vc=VC, chrt=11, barcode='source-code', title='Новый товар'):
    return {'nmID': nm, 'subjectID': 105, 'vendorCode': vc, 'title': title,
            'description': 'Описание', 'brand': 'Бренд', 'kizMarked': False,
            'dimensions': {'length': 10, 'width': 20, 'height': 30, 'weightBrutto': .5, 'isValid': True},
            'characteristics': [{'id': 10, 'name': 'Цвет', 'value': ['красный']}],
            'photos': [{'big': f'https://images.example/{nm}.webp'}], 'video': '',
            'sizes': [{'chrtID': chrt, 'techSize': '0', 'skus': [barcode]}],
            'documents': {'items': [{'id': f'doc-{nm}', 'type': 1, 'number': '123',
                                     'verdict': {'status': 1}}], 'excludeDocuments': False,
                          'overallVerdict': {'status': 1}}}


class TestOpenAPIPayloads(unittest.TestCase):
    def test_updates_use_target_identifiers_barcodes_and_preserve_unselected_content(self):
        source, target = card(), card(202, chrt=22, barcode='target-code', title='Старый товар')
        target['description'] = 'Оставить'
        before = copy.deepcopy(target)
        body, media = payloads.update_payload(source, target, {'title'})
        self.assertEqual((body['nmID'], body['sizes'][0]['chrtID']), (202, 22))
        self.assertEqual(body['sizes'][0]['skus'], ['target-code'])
        self.assertEqual(body['description'], 'Оставить')
        self.assertEqual(body['documents']['items'][0]['id'], 'doc-202')
        self.assertNotIn('verdict', body['documents']['items'][0])
        self.assertNotIn('isValid', body['dimensions'])
        self.assertIsNone(media)
        self.assertEqual(target, before)

    def test_photos_use_separate_media_request_and_preserve_video_or_block_hls(self):
        source, target = card(), card(202)
        target['video'] = 'https://images.example/keep.mp4'
        body, media = payloads.update_payload(source, target, {'photos'})
        self.assertIsNone(body)
        self.assertEqual(media, ['https://images.example/101.webp', target['video']])
        target['video'] = 'https://images.example/index.m3u8'
        with self.assertRaisesRegex(ValueError, 'HLS'):
            payloads.update_payload(source, target, {'photos'})
        # 非媒体更新不受已有 HLS 影响。
        payloads.update_payload(source, target, {'title'})

    def test_missing_fields_ambiguous_vc_category_and_spec_mismatch_are_blocked(self):
        for change in ({'vendorCode': 'other'}, {'subjectID': 999}, {'sizes': []}):
            target = card(202)
            target.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                payloads.update_payload(card(), target, {'title'})
        source = card()
        source.pop('description')
        with self.assertRaises(ValueError):
            payloads.update_payload(source, card(202), {'description'})
        with self.assertRaises(ValueError):
            payloads.card_index([card(), card(202)])
        target = card(202)
        target['sizes'].append({'chrtID': 33, 'techSize': 'S', 'skus': ['third']})
        with self.assertRaisesRegex(ValueError, '规格数量'):
            payloads.update_payload(card(), target, {'sizes'})

    def test_new_cards_do_not_copy_source_ids_barcodes_or_document_ids(self):
        source = card()
        body = payloads.create_payload(source, 59)
        variant = body['variants'][0]
        self.assertEqual(body['subjectID'], 105)
        self.assertEqual(variant['vendorCode'], VC)
        self.assertEqual(variant['sizes'], [{'techSize': '0', 'price': 59}])
        self.assertNotIn('nmID', variant)
        self.assertNotIn('id', variant['documents']['items'][0])
        self.assertNotIn('overallVerdict', variant['documents'])
        for price in (None, 0, -1, 1.5, True, float('inf')):
            with self.subTest(price=price), self.assertRaises(ValueError):
                payloads.create_payload(source, price)

    def test_multisize_updates_match_names_not_array_order(self):
        source, target = card(), card(202, chrt=22, barcode='target')
        source['sizes'] = [{'techSize': 'M', 'wbSize': '42', 'chrtID': 11, 'skus': ['a']},
                           {'techSize': 'L', 'wbSize': '44', 'chrtID': 12, 'skus': ['b']}]
        target['sizes'] = [{'techSize': 'L', 'wbSize': '44', 'chrtID': 99, 'skus': ['t1']},
                           {'techSize': 'M', 'wbSize': '42', 'chrtID': 98, 'skus': ['t2']}]
        body, _ = payloads.update_payload(source, target, {'title', 'sizes'})
        if body is not None:
            self.assertEqual([r['chrtID'] for r in body['sizes']], [99, 98])
        source['sizes'][0]['techSize'] = 'XL'
        with self.assertRaises(ValueError):
            payloads.update_payload(source, target, {'sizes'})


class TestOpenAPIWriteProtocol(unittest.TestCase):
    def api(self):
        api = WBContentAPI('fixture-secret')
        self.addCleanup(api.close)
        return api

    def test_readonly_whitelist_remains_readonly(self):
        api = WBOpenAPI('fixture-secret', 'cn')
        self.addCleanup(api.close)
        with patch.object(api.session, 'request') as request, self.assertRaises(ValueError):
            api._request('content-api', 'POST', '/content/v2/cards/upload', json=[])
        request.assert_not_called()

    def test_write_never_retries_timeout_5xx_or_business_error_and_redacts(self):
        for response in (requests.Timeout('fixture-secret'), Mock(status_code=504),
                         Mock(status_code=200, json=Mock(return_value={'error': True, 'errorText': 'fixture-secret'}))):
            api = self.api()
            kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
            with patch.object(api.session, 'request', **kwargs) as request:
                with self.assertRaises((NetworkError, PlatformApiError)) as raised:
                    api.create_cards([{'subjectID': 105, 'variants': []}])
                self.assertNotIn('fixture-secret', str(raised.exception))
                self.assertEqual(request.call_count, 1)

    def test_empty_204_is_accepted_and_exact_new_stock_fields_used(self):
        api = self.api()
        response = Mock(status_code=204)
        with patch.object(api.session, 'request', return_value=response) as request:
            result = api.set_stocks(42, [{'chrtId': 22, 'amount': 0}])
        self.assertTrue(result['accepted'])
        self.assertEqual(request.call_args.args, ('PUT', 'https://marketplace-api.wildberries.cn/api/v3/stocks/42'))
        self.assertEqual(request.call_args.kwargs['json'], {'stocks': [{'chrtId': 22, 'amount': 0}]})
        self.assertFalse(request.call_args.kwargs['allow_redirects'])
        response.json.assert_not_called()
        self.assertNotIn('Cookie', api.session.headers)

    def test_trash_response_without_error_flag_and_complete_cursor(self):
        api = self.api()
        response = Mock(status_code=200)
        response.json.return_value = {'cards': [], 'cursor': {'total': 0}}
        with patch.object(api.session, 'request', return_value=response):
            self.assertEqual(api.trash_cards(), [])
        with patch.object(api, '_content', side_effect=[
            {'cards': [card()], 'cursor': {'total': 1, 'trashedAt': 'stamp', 'nmID': 101}},
            {'cards': [], 'cursor': {'total': 0}},
        ]) as request:
            self.assertEqual(len(api.trash_cards(page_size=1)), 1)
            self.assertEqual(request.call_args.kwargs['body']['settings']['cursor']['trashedAt'], 'stamp')
        with patch.object(api, '_content', return_value={'cards': [card()], 'cursor': {'total': 1}}), self.assertRaises(PlatformApiError):
            api.trash_cards(page_size=1)

    def test_barcode_receipt_must_have_exact_unique_count(self):
        api = self.api()
        for values in ([], ['x'], ['x', 'x'], [None, 'x']):
            with patch.object(api, '_content', return_value={'data': values}), self.assertRaises(PlatformApiError):
                api.barcodes(2)


class TestOpenAPIWriteWorkflows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = paths.DATA_DIR
        self.addCleanup(paths.configure, previous)
        self.folder = Path(temporary.name)
        paths.configure(self.folder)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.output = io.StringIO()
        stack.enter_context(redirect_stdout(self.output))
        stack.enter_context(redirect_stderr(self.output))
        from sellerops.features.listings import open_api_common
        self.accesses = [OpenAPIShop(i, f'店{i}', f'fixture-secret-{i}') for i in (1, 2, 3)]
        stack.enter_context(patch.object(open_api_common.bcs.client, 'fetch_open_api_shops', return_value=self.accesses))
        self.events = []
        self.wrote = False
        self.invocation_wrote = False
        self.asset_wrote = False
        self.generated = {sid: [] for sid in (1, 2, 3)}
        self.clients = {}
        for i in (1, 2, 3):
            api = Mock(spec=WBContentAPI)
            self.clients[f'fixture-secret-{i}'] = api
            api.cards.side_effect = self.reader(f'cards-{i}', [card()] if i == 1 else [])
            api.warehouses.side_effect = self.reader(f'warehouse-{i}', [{'id': 40 + i, 'name': '莫斯科仓库', 'warehouseId': 9999}])
            api.trash_cards.side_effect = self.reader(f'trash-{i}', [])
            api.characteristics.side_effect = self.reader(f'category-{i}', SCHEMA)
            api.barcodes.side_effect = self.writer(f'barcodes-{i}', [f'new-{i}'])
            for name in ('create_cards', 'update_cards', 'save_media', 'set_stocks'):
                getattr(api, name).side_effect = self.writer(f'{name}-{i}', {'accepted': True})
        factory = lambda token: self.clients[token]
        for module in (publish, finish, sync):
            stack.enter_context(patch.object(module, 'WBContentAPI', side_effect=factory))

    def reader(self, name, value):
        def read(*args, **kwargs):
            if kwargs.get('vendor_code'):
                self.assertFalse(self.asset_wrote, f'图片/库存写后发生读取：{name}')
                self.assertEqual(kwargs['max_pages'], 10)
            else:
                self.assertFalse(self.invocation_wrote, f'写后发生非标识读取：{name}')
            self.events.append(name)
            if kwargs.get('vendor_code') and name.startswith('cards-'):
                sid = int(name.rsplit('-', 1)[1])
                return copy.deepcopy(self.generated[sid])
            return copy.deepcopy(value)
        return read

    def writer(self, name, value):
        def write(*args, **kwargs):
            self.wrote = True
            self.invocation_wrote = True
            if name.startswith(('save_media-', 'set_stocks-')):
                self.asset_wrote = True
            if name.startswith('create_cards-'):
                sid = int(name.rsplit('-', 1)[1])
                spec_offset = 0
                for i, group in enumerate(args[0]):
                    variant = group['variants'][0]
                    created = card(sid * 100 + 2 + i, vc=variant['vendorCode'])
                    created['subjectID'] = group['subjectID']
                    created['photos'] = []
                    created['sizes'] = []
                    for size in variant['sizes']:
                        created['sizes'].append(dict(size, chrtID=sid * 10 + 2 + spec_offset))
                        spec_offset += 1
                    self.generated[sid].append(created)
            self.events.append(name)
            return copy.deepcopy(value)
        return write

    def cli(self, *args):
        from sellerops.cli.main import main
        self.invocation_wrote = False
        self.asset_wrote = False
        return main(['--data-dir', str(self.folder), *args])

    def creation(self, *extra):
        return self.cli('publish-openapi', '--source-shop', '1', '--shops', '2', '--vc', VC,
                        '--price', '59', '--stock', '0', *extra)

    def test_preview_reads_entire_plan_but_does_not_generate_barcode_or_write_state(self):
        self.assertEqual(self.creation(), 0)
        self.assertFalse(self.wrote)
        self.assertFalse(store.state_path().exists())
        logs = list(Path(paths.LOG_DIR).glob('*.json'))
        plan = json.loads(logs[0].read_text(encoding='utf8'))['items'][0]
        self.assertEqual(plan['stock'], 0)
        self.assertEqual(plan['warehouse']['id'], 42)
        self.assertEqual(plan['card']['variants'][0]['vendorCode'], VC)

    def test_one_command_creates_waits_for_ids_and_submits_assets_without_final_reads(self):
        self.assertEqual(self.creation('--apply'), 0)
        api = self.clients['fixture-secret-2']
        created = api.create_cards.call_args.args[0][0]['variants'][0]
        self.assertEqual(created['sizes'][0]['skus'], ['new-2'])
        self.assertNotIn('chrtID', created['sizes'][0])
        self.assertTrue(store.legacy_submitted(2, VC))
        api.save_media.assert_called_once_with(202, ['https://images.example/101.webp'])
        api.set_stocks.assert_called_once_with(42, [{'chrtId': 22, 'amount': 0}])
        self.assertEqual(store.load_state()['items'][store.item_key(2, VC)]['status'], 'finished')
        self.assertEqual(self.events[-2:], ['save_media-2', 'set_stocks-2'])
        self.wrote = False
        self.assertEqual(self.cli('publish-openapi', '--resume', '--shops', '2', '--vc', VC, '--apply'), 0)
        self.assertEqual(api.set_stocks.call_count, 1)
        self.assertFalse(self.wrote)

    def test_unknown_create_is_recorded_and_cannot_be_resubmitted(self):
        api = self.clients['fixture-secret-2']
        api.create_cards.side_effect = requests.Timeout('fixture-secret-2')
        self.assertEqual(self.creation('--apply'), 1)
        self.assertEqual(store.load_state()['items'][store.item_key(2, VC)]['status'], 'unknown')
        self.wrote = False
        self.assertEqual(self.creation('--apply'), 1)
        self.assertEqual(api.create_cards.call_count, 1)
        self.assertNotIn('fixture-secret', self.output.getvalue() + store.state_path().read_text(encoding='utf8'))

    def test_timeout_can_resume_from_original_record_without_source_reads_or_recreating(self):
        api = self.clients['fixture-secret-2']
        api.cards.side_effect = self.reader('not-generated-yet', [])
        self.assertEqual(self.creation('--wait-timeout', '0', '--apply'), 1)
        self.assertEqual(store.load_state()['items'][store.item_key(2, VC)]['status'], 'accepted')
        source_reads = self.clients['fixture-secret-1'].cards.call_count
        api.cards.side_effect = self.reader('generated-now', self.generated[2])
        self.assertEqual(self.cli('publish-openapi', '--resume', '--shops', '2', '--vc', VC, '--apply'), 0)
        self.assertEqual(self.clients['fixture-secret-1'].cards.call_count, source_reads)
        self.assertEqual(api.barcodes.call_count, 1)
        self.assertEqual(api.create_cards.call_count, 1)
        api.set_stocks.assert_called_once_with(42, [{'chrtId': 22, 'amount': 0}])

    def test_rerun_original_command_resumes_accepted_record_instead_of_recreating(self):
        api = self.clients['fixture-secret-2']
        api.cards.side_effect = self.reader('not-generated-yet', [])
        self.assertEqual(self.creation('--wait-timeout', '0', '--apply'), 1)
        api.cards.side_effect = self.reader('generated-now', self.generated[2])
        self.assertEqual(self.creation('--apply'), 0)
        self.assertEqual(api.create_cards.call_count, 1)
        self.assertEqual(api.barcodes.call_count, 1)
        self.assertEqual(api.save_media.call_count, 1)

    def test_later_create_batch_failure_preserves_first_batch_and_stops_shop_continuation(self):
        rows = [card(1000 + i, vc=f'VC-{i}', chrt=100 + i, barcode=f'source-{i}') for i in range(101)]
        self.clients['fixture-secret-1'].cards.side_effect = self.reader('source-many', rows)
        api = self.clients['fixture-secret-2']
        api.barcodes.side_effect = lambda count: [f'new-{len(self.generated[2])}-{i}' for i in range(count)]
        first_batch = self.writer('create_cards-2', {'accepted': True})
        def create(batch):
            if api.create_cards.call_count == 1:
                return first_batch(batch)
            raise AuthenticationError('fixture-secret-2')
        api.create_cards.side_effect = create
        self.assertEqual(self.cli('publish-openapi', '--source-shop', '1', '--shops', '2',
                                  '--all', '--price', '59', '--apply'), 1)
        entries = list(store.load_state()['items'].values())
        self.assertEqual(sum(entry['status'] == 'accepted' for entry in entries), 100)
        self.assertEqual(sum(entry['status'] == 'unknown' for entry in entries), 1)
        api.save_media.assert_not_called()
        api.set_stocks.assert_not_called()
        self.assertFalse(any(call.kwargs.get('vendor_code') for call in api.cards.call_args_list))

    def test_multiple_products_and_all_specs_share_one_create_batch_with_unique_barcodes(self):
        first = card()
        first['sizes'].append({'chrtID': 12, 'techSize': 'S', 'skus': ['second-source']})
        second = card(102, vc='VC-SECOND', chrt=13, barcode='third-source')
        self.clients['fixture-secret-1'].cards.side_effect = self.reader('source-two', [first, second])
        api = self.clients['fixture-secret-2']
        api.barcodes.side_effect = self.writer('three-barcodes', ['new-a', 'new-b', 'new-c'])
        self.assertEqual(self.cli('publish-openapi', '--source-shop', '1', '--shops', '2', '--all',
                                  '--price', '59', '--apply'), 0)
        api.barcodes.assert_called_once_with(3)
        batch = api.create_cards.call_args.args[0]
        self.assertEqual(len(batch), 2)
        self.assertEqual([s['skus'] for group in batch for s in group['variants'][0]['sizes']],
                         [['new-a'], ['new-b'], ['new-c']])
        self.assertEqual(len(store.load_state()['items']), 2)

    def test_precreate_barcode_failure_can_be_retried_without_replaying_a_create(self):
        api = self.clients['fixture-secret-2']
        api.barcodes.side_effect = NetworkError('fixture-secret')
        self.assertEqual(self.creation('--apply'), 1)
        api.create_cards.assert_not_called()
        self.assertEqual(store.load_state()['items'][store.item_key(2, VC)]['status'], 'prepare_failed')
        api.barcodes.side_effect = self.writer('retry-barcodes', ['new-2'])
        self.assertEqual(self.creation('--apply'), 0)
        api.create_cards.assert_called_once()

    def test_trash_or_shared_bcs_record_prevents_new_card_without_clearing_state(self):
        api = self.clients['fixture-secret-2']
        api.trash_cards.side_effect = self.reader('trash-existing', [card(202)])
        self.assertEqual(self.creation('--apply'), 0)
        self.assertFalse(self.wrote)
        api.trash_cards.side_effect = self.reader('empty-trash', [])
        atomic_dump_json(Path(paths.STATE_DIR) / '复制上架记录.json', {VC: {'2': 'already-submitted'}})
        self.assertEqual(self.creation('--apply'), 0)
        api.barcodes.assert_not_called()
        api.create_cards.assert_not_called()

    def test_finish_stock_failure_preserves_media_acceptance_and_blocks_ambiguous_retry(self):
        api = self.clients['fixture-secret-2']
        api.set_stocks.side_effect = NetworkError('fixture-secret-2')
        self.assertEqual(self.creation('--apply'), 1)
        entry = store.load_state()['items'][store.item_key(2, VC)]
        self.assertEqual(entry['stages']['media']['status'], 'accepted')
        self.assertEqual(entry['stages']['stock']['status'], 'unknown')
        self.wrote = False
        self.assertEqual(self.cli('publish-openapi', '--resume', '--shops', '2', '--vc', VC, '--apply'), 1)
        self.assertEqual(api.save_media.call_count, 1)
        self.assertEqual(api.set_stocks.call_count, 1)

    def test_missing_moscow_blocks_only_that_shop_and_other_target_submission_is_preserved(self):
        self.clients['fixture-secret-2'].warehouses.side_effect = self.reader('bad-warehouse', [{'id': 9, 'name': '成都仓库'}])
        self.assertEqual(self.cli('publish-openapi', '--source-shop', '1', '--shops', '2,3', '--vc', VC,
                                  '--price', '59', '--apply'), 1)
        self.clients['fixture-secret-2'].create_cards.assert_not_called()
        self.clients['fixture-secret-3'].create_cards.assert_called_once()

    def test_public_nm_uses_cdn_characteristic_ids_and_explicit_vc_not_bcs_proxy(self):
        raw = {'subject_id': 105, 'imt_name': 'Товар', 'description': 'Описание',
               'selling': {'brand_name': 'Бренд'}, 'media': {'photo_count': 1},
               'options': [{'name': 'Цвет', 'value': 'красный'}]}
        with patch.object(sources, 'fetch_card_json', return_value=raw) as public:
            self.assertEqual(self.cli('publish-openapi', '--nm', '999', '--vc', 'BCS-TEST-{nm}', '--shops', '2',
                                      '--price', '59', '--dims', '10*20*30/0.5', '--apply'), 0)
        public.assert_called_once_with(999)
        variant = self.clients['fixture-secret-2'].create_cards.call_args.args[0][0]['variants'][0]
        self.assertEqual(variant['vendorCode'], VC)
        self.assertEqual(variant['characteristics'], [{'id': 10, 'value': ['красный']}])
        self.clients['fixture-secret-1'].cards.assert_not_called()

    def test_sync_default_excludes_source_and_uses_each_targets_own_ids(self):
        for sid in (2, 3):
            self.clients[f'fixture-secret-{sid}'].cards.side_effect = self.reader(
                f'target-{sid}', [card(sid * 100, chrt=sid * 10, barcode=f'target-{sid}', title='Старый')])
        self.assertEqual(self.cli('sync-cards-openapi', '--source-shop', '1', '--vc', VC, '--apply'), 0)
        self.clients['fixture-secret-1'].update_cards.assert_not_called()
        for sid in (2, 3):
            api = self.clients[f'fixture-secret-{sid}']
            body = api.update_cards.call_args.args[0][0]
            self.assertEqual((body['nmID'], body['sizes'][0]['chrtID']), (sid * 100, sid * 10))
            self.assertEqual(body['sizes'][0]['skus'], [f'target-{sid}'])
            api.save_media.assert_called_once_with(sid * 100, ['https://images.example/101.webp'])
            api.warehouses.assert_not_called()
            api.set_stocks.assert_not_called()

    def test_sync_partial_read_failure_is_nonzero_while_healthy_shop_is_submitted(self):
        self.clients['fixture-secret-2'].cards.side_effect = RuntimeError('fixture-secret-2')
        self.clients['fixture-secret-3'].cards.side_effect = self.reader('target3', [card(303, chrt=33, title='Старый')])
        self.assertEqual(self.cli('sync-cards-openapi', '--source-shop', '1', '--vc', VC, '--fields', 'title', '--apply'), 1)
        self.clients['fixture-secret-3'].update_cards.assert_called_once()
        self.assertNotIn('fixture-secret', self.output.getvalue())

    def test_sync_preview_has_no_writes_and_media_failure_keeps_content_receipt(self):
        api = self.clients['fixture-secret-2']
        api.cards.side_effect = self.reader('target', [card(202, chrt=22, title='Старый')])
        args = ('sync-cards-openapi', '--source-shop', '1', '--shops', '2', '--vc', VC)
        self.assertEqual(self.cli(*args), 0)
        self.assertFalse(self.wrote)
        api.save_media.side_effect = NetworkError('fixture-secret')
        self.assertEqual(self.cli(*args, '--apply'), 1)
        logs = [json.loads(path.read_text(encoding='utf8')) for path in Path(paths.LOG_DIR).glob('*.json')]
        row = next(doc['items'][0] for doc in logs if doc['apply'])
        self.assertEqual(row['status'], 'partial')
        self.assertTrue(row['cardReceipt']['accepted'])
        self.assertNotIn('fixture-secret', json.dumps(logs))

    def test_source_selection_typo_and_invalid_scope_do_not_expand_targets(self):
        api = self.clients['fixture-secret-2']
        for sid, vc in (('0', VC), ('1', 'not-found')):
            self.assertEqual(self.cli('sync-cards-openapi', '--source-shop', sid, '--vc', vc, '--apply'), 1)
        self.assertFalse(self.wrote)
        api.update_cards.assert_not_called()
        self.assertEqual(self.cli('publish-openapi', '--source-shop', '1', '--shops', '1',
                                  '--vc', VC, '--price', '59', '--apply'), 1)

    def test_public_file_can_supply_required_characteristics_and_multiple_sizes(self):
        file = self.folder / 'public.json'
        file.write_text(json.dumps([{'nmId': 999, 'vendorCode': VC, 'price': 59, 'card': {
            'title': 'Товар', 'dimensions': {'length': 10, 'width': 20, 'height': 30, 'weightBrutto': .5},
            'characteristics': [{'id': 10, 'value': ['красный']}],
            'sizes': [{'techSize': 'S'}, {'techSize': 'M'}],
        }}]), encoding='utf8')
        raw = {'subject_id': 105, 'imt_name': 'Товар', 'description': 'Описание',
               'media': {'photo_count': 1}}
        api = self.clients['fixture-secret-2']
        api.barcodes.side_effect = self.writer('two-barcodes', ['new-s', 'new-m'])
        with patch.object(sources, 'fetch_card_json', return_value=raw):
            self.assertEqual(self.cli('publish-openapi', '--file', str(file), '--shops', '2', '--apply'), 0)
        specs = api.create_cards.call_args.args[0][0]['variants'][0]['sizes']
        self.assertEqual([row['techSize'] for row in specs], ['S', 'M'])
        self.assertEqual([row['skus'] for row in specs], [['new-s'], ['new-m']])

    def test_wrong_spec_barcode_prevents_finish_and_missing_target_does_not_create(self):
        api = self.clients['fixture-secret-2']
        api.cards.side_effect = self.reader('not-generated-yet', [])
        self.assertEqual(self.creation('--wait-timeout', '0', '--apply'), 1)
        self.wrote = False
        api.cards.side_effect = self.reader('wrong-barcodes', [card(202)])
        self.assertEqual(self.cli('publish-openapi', '--resume', '--shops', '2', '--vc', VC, '--apply'), 1)
        api.set_stocks.assert_not_called()
        api.save_media.assert_not_called()
        api.cards.side_effect = self.reader('empty-target', [])
        self.assertEqual(self.cli('sync-cards-openapi', '--source-shop', '1', '--shops', '2', '--vc', VC, '--apply'), 0)
        api.update_cards.assert_not_called()

    def test_state_shop_identity_mismatch_is_rejected_before_any_submission(self):
        atomic_dump_json(store.state_path(), {'version': 1, 'items': {store.item_key(2, VC): {
            'shopId': 3, 'vc': VC, 'status': 'accepted', 'stages': {},
        }}})
        self.assertEqual(self.cli('publish-openapi', '--resume', '--shops', '2,3', '--vc', VC, '--apply'), 1)
        self.assertFalse(self.wrote)
        for api in self.clients.values():
            api.cards.assert_not_called()
            api.save_media.assert_not_called()
            api.set_stocks.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""历史 VC 改码的合成离线回归；不加载生产数据或连接平台。"""
import copy
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import openpyxl
import requests

from sellerops.core.files import atomic_dump_json
from sellerops.domain.vc_normalization import NormalizationCatalog, RenameAttempt, allocate_codes, replacement_base
from sellerops.features.listings import open_api_common, vc_normalization as feature
from sellerops.features.listings import vc_normalization_payloads as payloads
from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.integrations.wildberries.content_api import WBContentAPI
from sellerops.persistence import vc_normalization as store
from sellerops.settings import paths


OLD = 'BCS-RAND-123-2'
NEW = 'BCS-TEST-123-2'
CN = '示例商品'


def card(nm, vc=OLD):
    return {'nmID': nm, 'vendorCode': vc, 'subjectID': 105,
            'title': '原标题' * 30, 'description': '保留说明', 'brand': '',
            'sizes': [{'chrtID': nm * 10 + 1, 'techSize': 'M', 'wbSize': '40', 'skus': [f'code-{nm}-1', f'code-{nm}-2']},
                      {'chrtID': nm * 10 + 2, 'techSize': 'L', 'wbSize': '42', 'skus': [f'code-{nm}-3']}],
            'dimensions': {'length': 10.2, 'width': 0, 'height': 3.4, 'weightBrutto': 0, 'isValid': False},
            'characteristics': [{'id': 10, 'name': '颜色', 'value': ['红色']}],
            'documents': {'items': [{'id': f'doc-{nm}', 'type': 1, 'number': '123',
                                     'verdict': {'status': 1}}], 'excludeDocuments': False,
                          'overallVerdict': {'status': 1}}, 'gtin': '00123', 'kizMarked': False,
            'photos': [], 'video': 'https://example.invalid/video.m3u8', 'tags': [], 'updatedAt': 'now'}


class TestNormalizationRules(unittest.TestCase):
    def test_only_prefix_changes_in_all_supported_legacy_shapes(self):
        for old in (OLD, 'BCS-Abcd-ozon-card-123-2', 'BCS-ABCD-WRLINWI/123-2', 'BCS-ABCD/123', 'BCS-ABCD'):
            with self.subTest(old=old):
                self.assertEqual(replacement_base(old, 'TEST'), old[:4] + 'TEST' + old[8:])
        for old in ('BCS-ABCDE-123', 'other', ' BCS-ABCD-123'):
            with self.subTest(old=old), self.assertRaises(ValueError):
                replacement_base(old, 'TEST')

    def test_ownership_requires_agreement_and_override_wins(self):
        catalog = NormalizationCatalog(prefixes={CN: ['TEST']}, owners={OLD: {CN, '另一个商品'}})
        with self.assertRaisesRegex(ValueError, '分歧'):
            catalog.resolve(OLD)
        catalog.overrides[OLD] = {CN}
        self.assertEqual(catalog.resolve(OLD), (CN, 'TEST'))
        with self.assertRaisesRegex(ValueError, '缺少'):
            catalog.resolve('BCS-TEST-unknown')

    def test_duplicate_names_shared_prefix_and_invalid_prefix_are_blocked(self):
        for prefixes in ({CN: ['TEST', 'TEST']}, {CN: ['TEST'], '另一个商品': ['TEST']},
                         {CN: ['1234']}, {CN: ['']}, {}):
            with self.subTest(prefixes=prefixes), self.assertRaises(ValueError):
                NormalizationCatalog(prefixes=prefixes, owners={OLD: {CN}}).resolve(OLD)

    def test_colliding_candidates_receive_unique_deterministic_suffixes(self):
        other = 'BCS-ZZZZ-123-2'
        for occupied in (set(), {NEW.lower(), NEW + '-1'}):
            result, errors = allocate_codes({other: NEW, OLD: NEW}, occupied, [])
            self.assertFalse(errors)
            expected = (NEW, NEW + '-1') if not occupied else (NEW + '-2', NEW + '-3')
            self.assertEqual(result, {OLD: expected[0], other: expected[1]})

    def test_existing_code_collision_adds_a_number_without_replacing_original_tail(self):
        result, errors = allocate_codes({OLD: NEW}, {NEW.lower()}, [])
        self.assertEqual(result, {OLD: NEW + '-1'})
        self.assertFalse(errors)

    def test_history_assignment_is_reused_and_ambiguous_or_changed_history_is_blocked(self):
        history = [RenameAttempt(1, 101, OLD, NEW + '-3')]
        self.assertEqual(allocate_codes({OLD: NEW}, {NEW + '-3'}, history)[0][OLD], NEW + '-3')
        for prior in (history + [RenameAttempt(2, 202, OLD, NEW)],
                      [RenameAttempt(1, 101, OLD, 'BCS-NEXT-123-2')],
                      [RenameAttempt(1, 101, OLD, 'BCS-TEST-999-2-3')]):
            with self.subTest(prior=prior):
                self.assertIn(OLD, allocate_codes({OLD: NEW}, set(), prior)[1])

    def test_length_limit_does_not_truncate_original_code(self):
        base = 'BCS-TEST-' + 'x' * 63
        self.assertEqual(len(base), 72)
        self.assertIn(OLD, allocate_codes({OLD: base}, {base}, [])[1])
        with self.assertRaisesRegex(ValueError, '72'):
            replacement_base('BCS-RAND-' + 'x' * 64, 'TEST')

    def test_conflicting_historical_allocations_cannot_produce_duplicate_new_codes(self):
        other = 'BCS-ZZZZ-123-2'
        history = [RenameAttempt(1, 101, OLD, NEW), RenameAttempt(2, 202, other, NEW)]
        result, errors = allocate_codes({OLD: NEW, other: NEW}, set(), history)
        self.assertFalse(result)
        self.assertEqual(set(errors), {OLD, other})


class TestRenamePayload(unittest.TestCase):
    def test_untouched_editable_values_and_all_target_identifiers_are_preserved(self):
        source = card(101)
        before = copy.deepcopy(source)
        body = payloads.rename_payload(source, NEW)
        self.assertEqual(body['nmID'], 101)
        self.assertEqual(body['vendorCode'], NEW)
        for key in ('title', 'description', 'brand', 'sizes', 'gtin', 'kizMarked'):
            self.assertEqual(body[key], source[key])
        self.assertEqual(body['dimensions'], {'length': 10.2, 'width': 0, 'height': 3.4, 'weightBrutto': 0})
        self.assertEqual(body['documents']['items'][0]['id'], 'doc-101')
        self.assertNotIn('verdict', body['documents']['items'][0])
        self.assertNotIn('overallVerdict', body['documents'])
        self.assertEqual(body['characteristics'], [{'id': 10, 'value': ['红色']}])
        self.assertFalse({'photos', 'video', 'tags', 'updatedAt', 'subjectID'} & body.keys())
        body['sizes'][0]['skus'].append('changed-only-in-copy')
        self.assertEqual(source, before)

    def test_no_prices_photos_stock_or_dimensions_are_required(self):
        body = payloads.rename_payload({'nmID': 11, 'vendorCode': OLD, 'sizes': [{'chrtID': 22, 'skus': ['code']}]}, NEW)
        self.assertEqual(set(body), {'nmID', 'vendorCode', 'sizes'})

    def test_payload_preserves_original_tail_and_only_allows_appended_positive_sequence(self):
        for new in (NEW, NEW + '-1', NEW + '-23'):
            self.assertEqual(payloads.rename_payload(card(101), new)['vendorCode'], new)
        for new in ('BCS-TEST-999-2', 'BCS-TEST-123', NEW + '-0', NEW + '-01', NEW + '-1-2'):
            with self.subTest(new=new), self.assertRaisesRegex(ValueError, '原样保留'):
                payloads.rename_payload(card(101), new)

    def test_existing_duplicate_size_labels_do_not_require_cross_shop_matching(self):
        source = card(101)
        for size in source['sizes']:
            size.update(techSize='0', wbSize='0')
        body = payloads.rename_payload(source, NEW)
        self.assertEqual(body['sizes'], source['sizes'])

    def test_optional_empty_documents_preserve_the_explicit_exclusion_setting(self):
        for excluded in (False, True):
            for verdict in ({}, {'overallVerdict': {'status': 1}}):
                with self.subTest(excluded=excluded, verdict=verdict):
                    source = dict(card(101), documents=dict(excludeDocuments=excluded, **verdict))
                    before = copy.deepcopy(source)
                    body = payloads.rename_payload(source, NEW)
                    self.assertEqual(body['documents'], {'items': [], 'excludeDocuments': excluded})
                    self.assertEqual(source, before)

    def test_unknown_or_malformed_documents_cannot_be_assumed_empty(self):
        for document in (None, {}, {'overallVerdict': {}}, {'items': None},
                         {'excludeDocuments': 0}, {'excludeDocuments': False, 'unknown': ['certificate']}):
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, 'documents'):
                payloads.rename_payload(dict(card(101), documents=document), NEW)

    def test_incomplete_specs_barcodes_and_documents_cannot_be_overwritten(self):
        for change in ({'sizes': []}, {'sizes': [{'chrtID': 1, 'skus': []}]},
                       {'sizes': [{'chrtID': 1, 'skus': ['a']}, {'chrtID': 1, 'skus': ['b']}]},
                       {'documents': {'overallVerdict': {}}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                payloads.rename_payload(dict(card(1), **change), NEW)

    def test_batches_respect_card_count_and_actual_ascii_json_byte_size(self):
        self.assertEqual(payloads.batch_lengths([{'nmID': i} for i in range(201)]), [100, 100, 1])
        cards = [{'nmID': i, 'description': '中' * 60} for i in range(5)]
        with patch.object(payloads, 'MAX_REQUEST_BYTES', 850):
            lengths = payloads.batch_lengths(cards)
            self.assertEqual(lengths, [2, 2, 1])
            offset = 0
            for count in lengths:
                self.assertLessEqual(len(json.dumps(cards[offset:offset + count]).encode('utf8')), 850)
                offset += count
            with self.assertRaisesRegex(ValueError, '10 MB'):
                payloads.rename_payload(dict(card(1), description='中' * 1000), NEW)
        with self.assertRaises(ValueError):
            payloads.rename_payload(dict(card(1), dimensions={'length': float('nan')}), NEW)


class TestNormalizationCommand(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(paths.configure, paths.DATA_DIR)
        self.folder = Path(temporary.name)
        paths.configure(self.folder)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.output))
        self.price_rows = [(CN, 'TEST')]
        self.mapping_rows = [(OLD, CN)]
        self.write_books()
        self.shops = [OpenAPIShop(sid, f'店{sid}', f'fixture-secret-{sid}') for sid in (1, 2)]
        self.shop_fetch = self.stack.enter_context(patch.object(
            open_api_common.bcs.client, 'fetch_open_api_shops', return_value=self.shops))
        self.active = {1: [card(101)], 2: [card(202)]}
        self.trash = {1: [], 2: []}
        self.wrote = False
        self.events = []
        self.clients = {}
        for sid in (1, 2):
            api = Mock(spec=WBContentAPI)
            api.cards.side_effect = self.reader(sid, 'active')
            api.trash_cards.side_effect = self.reader(sid, 'trash')
            api.update_cards.side_effect = self.writer(sid)
            self.clients[sid] = api
        self.factory = self.stack.enter_context(patch.object(
            feature, 'WBContentAPI', side_effect=lambda key: self.clients[int(key.rsplit('-', 1)[1])]))

    def write_books(self, excluded=()):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Sheet1'
        ws.append(['vendorCode前缀码', '产品中文名'])
        for cn, prefix in self.price_rows:
            ws.append([prefix, cn])
        wb.save(paths.PRICE_BOOK_XLSX)
        wb.close()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '映射总表'
        ws.append(['vendorCode', '产品中文名'])
        for row in self.mapping_rows:
            ws.append(row)
        if excluded:
            ws = wb.create_sheet('已排除清单')
            ws.append(['vendorCode', '排除原因'])
            for vc in excluded:
                ws.append([vc, '人工排除'])
        wb.save(paths.MAPPING_XLSX)
        wb.close()

    def reader(self, sid, kind):
        def read(*args, **kwargs):
            self.assertFalse(self.wrote, '写入之后不得回读')
            self.events.append((kind, sid))
            return copy.deepcopy((self.active if kind == 'active' else self.trash)[sid])
        return read

    def writer(self, sid):
        def write(batch):
            self.wrote = True
            self.events.append(('write', sid))
            # 平台写入前，整个批次的 submitting 标识已经持久化。
            persisted = self.records()
            for body in batch:
                self.assertTrue(any(row.get('nmID') == body['nmID'] and row['status'] == 'submitting'
                                    for row in persisted))
            return {'accepted': True, 'taskId': 123}
        return write

    def cli(self, *args):
        from sellerops.cli.main import main
        self.wrote = False
        return main(['--data-dir', str(self.folder), 'normalize-vc-openapi', *args])

    def documents(self):
        return [json.loads(path.read_text(encoding='utf8')) for path in Path(paths.LOG_DIR).glob(f'{store.COMMAND}_*.json')]

    def records(self):
        return [row for doc in self.documents() for row in doc['items']]

    def test_preview_defaults_to_all_shops_and_leaves_inputs_and_state_unchanged(self):
        atomic_dump_json(paths.VC_KNOWN_JSON, {OLD: {'cn': CN}})
        atomic_dump_json(self.folder / 'products' / 'sentinel.json', {'not': 'a snapshot to load'})
        before = {p: p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        self.assertEqual(self.cli('--all'), 0)
        self.assertFalse(self.wrote)
        self.assertEqual(self.events, [('active', 1), ('trash', 1), ('active', 2), ('trash', 2)])
        rows = self.records()
        self.assertEqual([(r['shopId'], r['newVC'], r['status']) for r in rows], [(1, NEW, 'planned'), (2, NEW, 'planned')])
        for p, content in before.items():
            self.assertEqual(p.read_bytes(), content)
        self.assertEqual({p for p in self.folder.rglob('*') if p.is_file() and 'logs' not in p.parts}, set(before))
        for api in self.clients.values():
            api.update_cards.assert_not_called()
            api.warehouses.assert_not_called()
            api.prices.assert_not_called()
            api.stocks.assert_not_called()

    def test_apply_reads_all_shops_first_and_preserves_each_shops_identifiers(self):
        before = {p: p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        self.assertEqual(self.cli('--all', '--apply'), 0)
        self.assertEqual(self.events[:4], [('active', 1), ('trash', 1), ('active', 2), ('trash', 2)])
        for sid, nm in ((1, 101), (2, 202)):
            body = self.clients[sid].update_cards.call_args.args[0][0]
            self.assertEqual((body['nmID'], body['vendorCode']), (nm, NEW))
            self.assertEqual(body['sizes'], self.active[sid][0]['sizes'])
            self.clients[sid].save_media.assert_not_called()
            self.clients[sid].set_stocks.assert_not_called()
        self.assertTrue(all(row['status'] == 'accepted' for row in self.records()))
        self.assertTrue(all(row['cardReceipt']['taskId'] == 123 for row in self.records()))
        logs = ''.join(p.read_text(encoding='utf8') for p in Path(paths.LOG_DIR).glob('*.json'))
        self.assertNotIn('fixture-secret', self.output.getvalue() + logs)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(Path(paths.STATE_DIR).exists())

    def test_selected_shops_and_name_or_vc_limit_the_target_without_inference(self):
        self.assertEqual(self.cli('--shops', '2', '--name', '示例', '--apply'), 0)
        self.clients[1].cards.assert_not_called()
        self.clients[2].update_cards.assert_called_once()
        self.assertEqual(len(self.records()), 1)

    def test_specified_vc_missing_in_one_shop_is_skipped_without_cross_shop_id_borrowing(self):
        self.active[2] = []
        self.assertEqual(self.cli('--vc', OLD, '--apply'), 0)
        self.clients[2].update_cards.assert_not_called()
        self.assertEqual({r['status'] for r in self.records()}, {'skipped', 'accepted'})

    def test_missing_or_conflicting_ownership_blocks_but_manual_override_wins(self):
        atomic_dump_json(paths.VC_KNOWN_JSON, {OLD: {'cn': '不同商品'}})
        self.assertEqual(self.cli('--all'), 1)
        self.assertTrue(all('分歧' in r['reason'] for r in self.records()))
        atomic_dump_json(paths.VC_OVERRIDE_JSON, {OLD.lower(): {'cn': CN}})
        self.assertEqual(self.cli('--all'), 0)
        self.assertFalse(self.wrote)

    def test_random_prefix_and_matching_title_never_supply_ownership(self):
        self.mapping_rows = []
        self.write_books()
        for rows in self.active.values():
            rows[0]['title'] = CN
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertFalse(self.wrote)
        self.assertTrue(all('缺少明确' in r['reason'] for r in self.records()))

    def test_exclusion_and_already_correct_codes_are_normal_skips(self):
        self.write_books(excluded=[OLD])
        self.assertEqual(self.cli('--all', '--apply'), 0)
        self.assertFalse(self.wrote)
        self.assertTrue(all('排除' in r['reason'] for r in self.records()))
        self.mapping_rows = [(NEW, CN)]
        self.write_books()
        self.active = {1: [card(101, NEW)], 2: [card(202, NEW)]}
        self.assertEqual(self.cli('--all'), 0)
        self.assertTrue(any('正确前缀' in r.get('reason', '') for r in self.records()))

    def test_conflicts_across_shops_append_consistent_suffixes_and_preserve_original_tail(self):
        other = 'BCS-ZZZZ-123-2'
        self.mapping_rows.append((other, CN))
        self.write_books()
        self.active[1].append(card(103, other))
        self.active[2].append(card(203, other))
        self.active[1].append(card(104, NEW))
        self.mapping_rows.append((NEW, CN))
        self.write_books()
        self.trash[2] = [dict(card(299, NEW + '-1'), trashedAt='yesterday')]
        self.assertEqual(self.cli('--all', '--apply'), 0)
        accepted = [r for r in self.records() if r['status'] == 'accepted']
        self.assertEqual(len(accepted), 4)
        self.assertEqual({r['newVC'] for r in accepted if r['vc'] == OLD}, {NEW + '-2'})
        self.assertEqual({r['newVC'] for r in accepted if r['vc'] == other}, {NEW + '-3'})

    def test_trash_occupancy_causes_the_same_appended_number_in_every_shop(self):
        self.trash[2] = [dict(card(299, NEW), trashedAt='yesterday')]
        self.assertEqual(self.cli('--vc', OLD, '--apply'), 0)
        self.assertEqual({row['newVC'] for row in self.records()}, {NEW + '-1'})

    def test_any_shop_read_failure_aborts_entire_run_and_sanitizes_remote_errors(self):
        self.clients[2].trash_cards.side_effect = requests.Timeout('fixture-secret-2')
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertFalse(self.wrote)
        for api in self.clients.values():
            api.update_cards.assert_not_called()
        self.assertTrue(self.documents()[0]['aborted'])
        self.assertNotIn('fixture-secret', self.output.getvalue() + json.dumps(self.documents()))

    def test_duplicate_or_invalid_remote_identity_aborts_before_any_write(self):
        self.trash[2] = [card(202, 'different-vc')]
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertFalse(self.wrote)

    def test_missing_key_stops_the_entire_batch(self):
        def factory(key):
            if key.endswith('-2'):
                raise ValueError('缺少有效 WB Open API 令牌')
            return self.clients[1]
        self.factory.side_effect = factory
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertFalse(self.wrote)

    def test_incomplete_card_is_blocked_and_other_prepared_cards_can_submit(self):
        self.active[1][0]['sizes'] = []
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.clients[1].update_cards.assert_not_called()
        self.clients[2].update_cards.assert_called_once()

    def test_omitted_optional_document_items_do_not_block_the_whole_operation(self):
        for cards in self.active.values():
            cards[0]['documents'] = {'excludeDocuments': False, 'overallVerdict': {'status': 1}}
        self.assertEqual(self.cli('--all', '--apply'), 0)
        for api in self.clients.values():
            body = api.update_cards.call_args.args[0][0]
            self.assertEqual(body['documents'], {'items': [], 'excludeDocuments': False})
        self.assertTrue(all(row['status'] == 'accepted' for row in self.records()))

    def test_accepted_and_unknown_attempts_cannot_be_replayed_even_with_new_occupancy(self):
        self.clients[2].update_cards.side_effect = requests.Timeout('fixture-secret-2')
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.active[1].append(card(199, NEW + '-1'))
        self.events.clear()
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertFalse(self.wrote)
        self.assertEqual([api.update_cards.call_count for api in self.clients.values()], [1, 1])
        self.assertEqual({a.new_vc for a in store.load_attempts()}, {NEW})

    def test_interrupted_submitting_log_prevents_replay(self):
        self.clients[1].update_cards.side_effect = KeyboardInterrupt()
        self.assertEqual(self.cli('--all', '--apply'), 130)
        self.assertEqual(len(store.load_attempts()), 1)
        self.clients[1].update_cards.side_effect = self.writer(1)
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertEqual(self.clients[1].update_cards.call_count, 1)
        self.clients[2].update_cards.assert_called_once()

    def test_receipt_save_failure_leaves_submitting_guard_and_stops_all_remaining_writes(self):
        original_save = open_api_common.OperationLog.save
        def save(log):
            if any(row['status'] == 'accepted' for row in log.document['items']):
                raise OSError('fixture save failure')
            return original_save(log)
        with patch.object(open_api_common.OperationLog, 'save', save):
            self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertTrue(any(row['status'] == 'submitting' for row in self.records()))
        self.clients[2].update_cards.assert_not_called()
        self.assertEqual(self.cli('--vc', OLD, '--shops', '1', '--apply'), 1)
        self.assertEqual(self.clients[1].update_cards.call_count, 1)

    def test_unconfirmed_api_receipt_is_unknown_and_cannot_be_replayed(self):
        self.clients[1].update_cards.side_effect = None
        self.clients[1].update_cards.return_value = {'accepted': False}
        self.assertEqual(self.cli('--vc', OLD, '--shops', '1', '--apply'), 1)
        self.assertEqual(self.records()[0]['status'], 'unknown')
        self.assertEqual(self.cli('--vc', OLD, '--shops', '1', '--apply'), 1)
        self.assertEqual(self.clients[1].update_cards.call_count, 1)

    def test_later_batch_failure_keeps_receipts_stops_shop_and_keeps_other_shop_allocation(self):
        self.active[1] = [card(1000 + i, f'BCS-RAND-{i:04}') for i in range(205)]
        self.mapping_rows = [(c['vendorCode'], CN) for c in self.active[1]] + [(OLD, CN)]
        self.write_books()
        good = self.writer(1)
        def write(batch):
            if self.clients[1].update_cards.call_count == 2:
                raise requests.Timeout('fixture-secret-1')
            return good(batch)
        self.clients[1].update_cards.side_effect = write
        self.assertEqual(self.cli('--all', '--apply'), 1)
        statuses = [r['status'] for r in self.records() if r['shopId'] == 1]
        self.assertEqual(statuses.count('accepted'), 100)
        self.assertEqual(statuses.count('unknown'), 100)
        self.assertEqual(statuses.count('blocked'), 5)
        self.clients[2].update_cards.assert_called_once()
        self.assertTrue(all(len(call.args[0]) <= 100 for call in self.clients[1].update_cards.call_args_list))

    def test_pending_shop_reuses_history_code_when_completed_shop_now_has_new_vc(self):
        atomic_dump_json(Path(paths.LOG_DIR) / f'{store.COMMAND}_previous.json', {
            'version': 1, 'command': store.COMMAND, 'apply': True, 'items': [
                {'shopId': 1, 'nmID': 101, 'vc': OLD, 'newVC': NEW, 'status': 'accepted'}]})
        self.active[1] = [card(101, NEW)]
        self.assertEqual(self.cli('--vc', OLD, '--apply'), 0)
        self.clients[1].update_cards.assert_not_called()
        self.assertEqual(self.clients[2].update_cards.call_args.args[0][0]['vendorCode'], NEW)

    def test_historical_numbered_allocation_is_reused_by_pending_shops(self):
        atomic_dump_json(Path(paths.LOG_DIR) / f'{store.COMMAND}_previous.json', {
            'version': 1, 'command': store.COMMAND, 'apply': True, 'items': [
                {'shopId': 1, 'nmID': 101, 'vc': OLD, 'newVC': NEW + '-3', 'status': 'accepted'}]})
        self.active[1] = [card(101, NEW + '-3')]
        self.assertEqual(self.cli('--vc', OLD, '--apply'), 0)
        self.clients[1].update_cards.assert_not_called()
        self.assertEqual(self.clients[2].update_cards.call_args.args[0][0]['vendorCode'], NEW + '-3')

    def test_history_target_taken_by_unrelated_card_is_not_reallocated(self):
        atomic_dump_json(Path(paths.LOG_DIR) / f'{store.COMMAND}_previous.json', {
            'version': 1, 'command': store.COMMAND, 'apply': True, 'items': [
                {'shopId': 1, 'nmID': 101, 'vc': OLD, 'newVC': NEW, 'status': 'unknown'}]})
        self.trash[2] = [card(299, NEW)]
        self.assertEqual(self.cli('--vc', OLD, '--apply'), 1)
        self.assertFalse(self.wrote)

    def test_corrupt_inputs_and_history_fail_closed_without_silently_emptying_them(self):
        for path in (Path(paths.VC_KNOWN_JSON), Path(paths.LOG_DIR) / f'{store.COMMAND}_corrupt.json'):
            with self.subTest(path=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{broken', encoding='utf8')
                self.assertEqual(self.cli('--all', '--apply'), 1)
                self.assertFalse(self.wrote)
                self.assertEqual(path.read_text(encoding='utf8'), '{broken')
                path.unlink()

    def test_history_with_missing_ids_or_unknown_status_fails_closed(self):
        path = Path(paths.LOG_DIR) / f'{store.COMMAND}_invalid.json'
        for row in ({'status': 'accepted'}, {'status': 'accpted'}, {'status': 'submitting', 'shopId': True}):
            with self.subTest(row=row):
                atomic_dump_json(path, {'version': 1, 'command': store.COMMAND, 'apply': True, 'items': [row]})
                self.assertEqual(self.cli('--all', '--apply'), 1)
                self.assertFalse(self.wrote)
                path.unlink()

    def test_duplicate_mapping_ownership_is_not_silently_resolved_by_last_row(self):
        self.mapping_rows.append((OLD, '另一个商品'))
        self.write_books()
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.assertFalse(self.wrote)
        self.assertTrue(all('分歧' in row['reason'] for row in self.records()))

    def test_known_records_alone_can_supply_ownership_and_json_exclusion_wins(self):
        Path(paths.MAPPING_XLSX).unlink()
        atomic_dump_json(paths.VC_KNOWN_JSON, {OLD: {'cn': CN}})
        self.assertEqual(self.cli('--all'), 0)
        atomic_dump_json(paths.VC_EXCLUDED_JSON, {OLD.lower(): {'reason': '人工排除'}})
        self.assertEqual(self.cli('--all', '--apply'), 0)
        self.assertFalse(self.wrote)

    def test_price_book_missing_required_prefix_header_fails_before_remote_reads(self):
        wb = openpyxl.Workbook()
        wb.active.title = 'Sheet1'
        wb.active.append(['产品中文名'])
        wb.save(paths.PRICE_BOOK_XLSX)
        wb.close()
        self.assertEqual(self.cli('--all', '--apply'), 1)
        self.shop_fetch.assert_not_called()
        self.assertFalse(self.wrote)

    def test_invalid_selectors_do_not_read_credentials_or_platform(self):
        self.assertEqual(self.cli('--vc', ',bad', '--apply'), 1)
        self.assertEqual(self.cli('--shops', '1,01', '--all'), 1)
        self.assertEqual(self.cli('--name', ' '), 1)
        self.shop_fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()

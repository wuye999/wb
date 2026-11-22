
"""Offline regressions for new listing ownership and the WorkBuddy JSON protocol."""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import PropertyMock, patch

from catalog_fixture import map_vcs, snapshot, write_book
from sellerops.cli.main import build_parser, leaf_commands, main
from sellerops.features.catalog import products
from sellerops.features.products import warehouse
from sellerops.features.publishing import bulk_publish, card_publish, publish_details
from sellerops.features.system import doctor
from sellerops.persistence import db
from sellerops.settings import paths


FAMILIES = {'doctor', 'sync', 'shop', 'map', 'ops', 'apply', 'report'}


class Step3Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(paths.configure, paths.current.data_dir)
        self.root = Path(temp.name)
        self.data = self.root / 'data'
        paths.configure(self.data)
        write_book([('S1', '旅行杯', 39, 'ABCD', 'INV1'),
                    ('S2', '保温袋', 48, '', 'INV2'),
                    ('S3', '名称重复', 52, 'DUPL', ''),
                    ('S4', '名称重复', 49, 'DUPL', '')])
        snapshot(1, [])
        snapshot(2, [])

    def cli(self, *args):
        output, logs = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(logs):
            code = main(['--data-dir', str(self.data), *args, '--json'])
        self.assertEqual(len(output.getvalue().splitlines()), 1, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {'ok', 'summary', 'plan_id', 'data', 'next_action'})
        self.assertIs(result['ok'], code == 0)
        self.assertIsInstance(result['data'], dict)
        self.assertTrue(result['summary'])
        if code:
            self.assertTrue(result['next_action'], result)
        return code, result, logs.getvalue()

    def preview(self, action, nm, *args):
        return self.cli('ops', action, str(nm), '--shops', '1,2', '--dims', '2*3*4/0.5',
                        '--stock', '0', *args)

    def item_from_plan(self, action, plan_id):
        with db.connect() as conn:
            row = conn.execute('SELECT status, payload_json FROM plans WHERE plan_id=?', (plan_id,)).fetchone()
        [item] = json.loads(row['payload_json'])['scope']['items']
        return item['item'] if action == 'publish' else item[0]

    def patch_publish(self, *, response=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        bcs = bulk_publish.bcs
        stack.enter_context(patch.object(type(bcs), 'base_url', new_callable=PropertyMock,
                                         return_value='https://offline.invalid'))
        stack.enter_context(patch.object(bcs, 'fetch_shop_list', return_value=[{'id': 1, 'name': '店1'}, {'id': 2, 'name': '店2'}]))
        for module in (bulk_publish, card_publish, warehouse):
            stack.enter_context(patch.object(module, 'resolve_warehouse', side_effect=lambda sid: 40 + sid))
        stack.enter_context(patch.object(bulk_publish, 'vc_exists_in_shop', return_value=False))
        stack.enter_context(patch.object(publish_details, 'fetch_card_json', return_value={
            'imt_name': 'Synthetic card', 'data': {'subject_id': 1}}))
        accepted = {'code': 200, 'data': {'taskId': 'offline'}}
        bulk = stack.enter_context(patch.object(bcs, 'batch_push_products', return_value=response or accepted))
        card = stack.enter_context(patch.object(bcs, 'post', return_value=response or accepted))
        return bulk, card

    def test_registered_prefix_comes_from_sku_and_apply_maps_accepted_vc(self):
        bulk, card = self.patch_publish()
        for action, nm, send in (('publish', 101, bulk), ('create-cards', 102, card)):
            with self.subTest(action=action):
                code, draft, _ = self.preview(action, nm, '--sku', 'S1')
                self.assertEqual(code, 0, draft)
                vc = f'BCS-ABCD-{nm}'
                item = self.item_from_plan(action, draft['plan_id'])
                self.assertEqual((item['vendor_code'], item['extra']['vc_source']), (vc, 'registered_prefix'))
                self.assertEqual(db.resolve_vc(vc)['status'], 'pending')
                send.assert_not_called()
                code, result, _ = self.cli('apply', '--plan', draft['plan_id'])
                self.assertEqual((code, result['data']['status']), (0, 'done'), result)
                owner = db.resolve_vc(vc)
                self.assertEqual((owner['sku'], owner['source']), ('S1', 'publish'))
                send.assert_called_once()
                if action == 'publish':
                    configs, rows = send.call_args.args
                    self.assertEqual([(s['id'], s['warehouseId']) for s in configs], [(1, 41), (2, 42)])
                    self.assertEqual(rows[0]['vendorCodePrefix'], 'BCS-ABCD')
                else:
                    payload = send.call_args.args[1]
                    self.assertEqual(payload['shopDatas'][0]['vendorCode'], vc)
                self.assertEqual(self.cli('apply', '--plan', draft['plan_id'])[0], 1)
                send.assert_called_once()

    def test_custom_vc_is_preserved_or_blocked_before_bulk_write(self):
        bulk, card = self.patch_publish()
        full = 'My-Exact-vc-54321'
        code, blocked, _ = self.preview('publish', 103, '--sku', 'S1', '--vc', full, '--prefix', 'OVER')
        self.assertEqual(code, 1)
        self.assertIsNone(blocked['plan_id'])
        self.assertIn('create-cards', blocked['next_action'])
        code, draft, _ = self.preview('create-cards', 103, '--sku', 'S1', '--vc', full, '--prefix', 'OVER')
        self.assertEqual(code, 0, draft)
        item = self.item_from_plan('create-cards', draft['plan_id'])
        self.assertEqual((item['vendor_code'], item['extra']['vc_source']), (full, 'explicit_vc'))
        self.assertEqual(self.cli('apply', '--plan', draft['plan_id'])[0], 0)
        self.assertEqual(card.call_args.args[1]['shopDatas'][0]['vendorCode'], full)
        bulk.assert_not_called()

    def test_explicit_full_code_and_explicit_prefix_win_over_registered_prefix(self):
        bulk, card = self.patch_publish()
        for nm, selection, expected, source in (
                (111, ('--vc', 'BCS-KEEP-111', '--prefix', 'OVER'), 'BCS-KEEP-111', 'explicit_vc'),
                (112, ('--prefix', 'BCS-OVER'), 'BCS-OVER-112', 'explicit_prefix')):
            with self.subTest(nm=nm):
                code, draft, _ = self.preview('publish', nm, '--sku', 'S1', '--random-prefix', *selection)
                self.assertEqual(code, 0, draft)
                item = self.item_from_plan('publish', draft['plan_id'])
                self.assertEqual((item['vendor_code'], item['extra']['vc_source']), (expected, source))
        bulk.assert_not_called()
        card.assert_not_called()

    def test_missing_or_ambiguous_ownership_and_prefix_require_input(self):
        bulk, card = self.patch_publish()
        for options, hint in ((('--sku', 'S2'), '--prefix'), (('--sku', 'S3'), '--prefix'),
                              (('--cn', '名称重复', '--prefix', 'OVER'), '--sku')):
            with self.subTest(options=options):
                code, result, _ = self.preview('publish', 104, *options)
                self.assertEqual(code, 1)
                self.assertIsNone(result['plan_id'])
                self.assertIn(hint, result['next_action'])
        bulk.assert_not_called()
        card.assert_not_called()

    def test_file_inputs_preserve_explicit_codes_and_detect_conflicting_assignments(self):
        bulk, card = self.patch_publish()
        file = self.root / 'publish.json'
        file.write_text(json.dumps([{'nmId': 101, 'sellerSku': 'S1'}]), encoding='utf8')
        args = ('ops', 'create-cards', '--shops', '1', '--dims', '2*3*4/0.5', '--file', str(file))
        code, draft, _ = self.cli(*args, '--vc', 'Exact-{nm}', '--random-prefix')
        self.assertEqual(code, 0, draft)
        self.assertEqual(self.item_from_plan('create-cards', draft['plan_id'])['vendor_code'], 'Exact-101')
        file.write_text(json.dumps([{'nmId': 103, 'sellerSku': 'S1', 'vendorCode': 'Same-123'},
                                    {'nmId': 104, 'sellerSku': 'S2', 'vendorCode': 'Same-123'}]), encoding='utf8')
        code, reply, _ = self.cli(*args)
        self.assertEqual(code, 1)
        self.assertEqual(len(reply['data']['result']['blocked']), 2)
        self.assertIsNone(reply['plan_id'])
        bulk.assert_not_called()
        card.assert_not_called()

    def test_random_value_is_frozen_and_timeout_never_maps_or_replays(self):
        bulk, _ = self.patch_publish()
        bulk.side_effect = TimeoutError('offline')
        with patch.object(publish_details, 'random_prefix', return_value='RAND') as draw:
            code, draft, _ = self.preview('publish', 105, '--sku', 'S2', '--random-prefix')
            self.assertEqual(code, 0, draft)
            item = self.item_from_plan('publish', draft['plan_id'])
            self.assertEqual((item['prefix'], item['extra']['vc_source']), ('BCS-RAND', 'random'))
            code, receipt, _ = self.cli('apply', '--plan', draft['plan_id'])
            self.assertEqual((code, receipt['data']['status']), (1, 'unknown'))
            self.assertEqual(self.cli('apply', '--plan', draft['plan_id'])[0], 1)
            draw.assert_called_once()
        self.assertEqual(bulk.call_count, 1)
        configs, rows = bulk.call_args.args
        self.assertEqual(len(configs), 2)
        self.assertEqual(rows[0]['vendorCodePrefix'], 'BCS-RAND')
        self.assertEqual(db.resolve_vc('BCS-RAND-105')['status'], 'pending')

    def test_rejected_cards_do_not_gain_ownership_and_existing_manual_owner_is_preserved(self):
        _, card = self.patch_publish(response={'code': 400, 'msg': 'offline rejection'})
        code, draft, _ = self.preview('create-cards', 106, '--sku', 'S1')
        self.assertEqual(code, 0, draft)
        code, applied, _ = self.cli('apply', '--plan', draft['plan_id'])
        self.assertEqual((code, applied['data']['status']), (1, 'partial'))
        self.assertEqual(db.resolve_vc('BCS-ABCD-106')['status'], 'pending')
        map_vcs({'BCS-ABCD-107': 'S1'})
        card.return_value = {'code': 200}
        code, draft, _ = self.preview('create-cards', 107, '--vc', 'BCS-ABCD-107')
        self.assertEqual(code, 0, draft)
        self.assertEqual(self.cli('apply', '--plan', draft['plan_id'])[0], 0)
        self.assertEqual(db.resolve_vc('BCS-ABCD-107')['source'], 'manual')

    def test_old_unsubmitted_publish_plan_requires_a_new_preview(self):
        bulk, _ = self.patch_publish()
        code, draft, _ = self.preview('publish', 109, '--sku', 'S1')
        self.assertEqual(code, 0, draft)
        plan_id = draft['plan_id']
        with db.connect(write=True) as conn:
            payload = conn.execute('SELECT payload_json FROM plans WHERE plan_id=?', (plan_id,)).fetchone()[0]
            document = json.loads(payload)
            document['scope']['items'][0]['item']['extra'].pop('vc_source')
            db.save_plan(conn, plan_id, 'publish', 'draft', plan_id, document)
        code, result, _ = self.cli('apply', '--plan', plan_id)
        self.assertEqual(code, 1)
        self.assertIn('重新预览', result['summary'])
        bulk.assert_not_called()

    def test_accepted_receipt_preserves_concurrent_manual_rebind(self):
        bulk, _ = self.patch_publish()
        code, draft, _ = self.preview('publish', 108, '--sku', 'S1')
        self.assertEqual(code, 0)
        vc = 'BCS-ABCD-108'

        def accept(*_, **options):
            map_vcs({vc: 'S2'})
            return {'code': 200, 'data': {'taskId': 'accepted'}}

        bulk.side_effect = accept
        code, result, _ = self.cli('apply', '--plan', draft['plan_id'])
        self.assertEqual((code, result['data']['status']), (1, 'partial'))
        self.assertEqual(result['data']['receipt']['mapping_conflicts'], [vc])
        self.assertEqual(result['data']['receipt']['items'][0]['task_id'], 'accepted')
        self.assertEqual(db.resolve_vc(vc)['sku'], 'S2')
        self.assertEqual(self.cli('apply', '--plan', draft['plan_id'])[0], 1)
        bulk.assert_called_once()

    def test_all_seven_families_emit_one_json_for_success_and_invalid_parameters(self):
        actions = [('doctor',), ('sync', '--all', '--no-sync'), ('shop', 'list'),
                   ('map', 'candidates'), ('ops', 'publish', '121', '--sku', 'S1', '--shops', '1,2',
                                           '--dims', '2*3*4/0.5'),
                   ('report', 'mapping')]
        self.patch_publish()
        with patch.object(products.bcs, 'fetch_shop_list', return_value=[{'id': 1, 'name': '店1'}, {'id': 2, 'name': '店2'}]), \
                patch.object(products.bcs, 'fetch_shop_products', return_value=[]):
            for args in actions:
                with self.subTest(args=args):
                    code, reply, logs = self.cli(*args)
                    self.assertEqual(code, 0, (reply, logs))
                    if args[0] == 'ops':
                        plan_id = reply['plan_id']
                    if args[0] == 'report':
                        self.assertTrue(Path(reply['data']['artifacts']['workbook']).is_file())
                        self.assertIn('已导出', logs)
        args = ('apply', '--plan', plan_id)
        self.assertEqual(self.cli(*args)[0], 0)
        for args in [*actions, args]:
            with self.subTest(invalid=args):
                code, reply, _ = self.cli(*args, '--bad-argument')
                self.assertEqual(code, 2)
                self.assertIn('无法识别参数', reply['summary'])

    def test_each_public_leaf_accepts_json_and_help_is_structured(self):
        families = set()
        for name, parser in leaf_commands(build_parser()):
            family = name.split()[0]
            if family in FAMILIES:
                families.add(family)
                with self.subTest(command=name):
                    self.assertTrue(any('--json' in action.option_strings for action in parser._actions))
                    self.assertIn('usage:', self.cli(*name.split(), '--help')[1]['data']['help'])
        self.assertEqual(families, FAMILIES)

    def test_json_failure_paths_include_partial_sync_and_missing_plan(self):
        with patch.object(products.bcs, 'fetch_shop_list', return_value=[{'id': 1, 'name': '店1'}, {'id': 2, 'name': '店2'}]), \
                patch.object(products.bcs, 'fetch_shop_products', side_effect=[[], ValueError('离线店铺失败')]):
            code, reply, _ = self.cli('sync', '--all', '--no-sync', '--parallel', '1')
        self.assertEqual(code, 1)
        self.assertTrue(reply['data']['errors'])
        self.assertIn('部分失败', reply['summary'])
        code, reply, _ = self.cli('apply', '--plan', 'P999')
        self.assertEqual(code, 1)
        self.assertIn('找不到计划单', reply['summary'])
        self.assertEqual(reply['plan_id'], 'P999')
        book = Path(paths.current.price_book_xlsx)
        original = book.read_bytes()
        code, reply, _ = self.cli('report', 'products', '--out', str(book))
        self.assertEqual(code, 1)
        self.assertIn('只读', reply['summary'])
        self.assertEqual(book.read_bytes(), original)
        with patch.object(doctor, 'run', side_effect=KeyboardInterrupt):
            self.assertEqual(self.cli('doctor')[0], 130)

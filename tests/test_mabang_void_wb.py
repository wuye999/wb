"""Offline coverage of WB supply evidence, Mabang preflight and write receipts."""
from contextlib import ExitStack, redirect_stdout
import csv
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

import requests

from sellerops.cli.main import build_parser
from sellerops.features.orders import mabang_void_wb as void
from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.integrations.mabang import orders
from sellerops.integrations.wildberries.fbs_api import FBSRequestError, WBFBSAPI
from sellerops.persistence import fbs_shipments, mabang_voids
from sellerops.persistence.price_book import SHIPPING_MODE_MABANG, SHIPPING_MODE_WB
from sellerops.settings import paths


SUPPLY = 'WB-GI-100001'
OLD = 'WB-GI-100002'
SECOND = 'WB-GI-100003'
PRODUCT = '示例商品'
VC = 'BCS-DEMO-1'


def supply(ids=(101, 102), *, shop=1, number=SUPPLY, stage='delivered'):
    return {
        'shop_id': shop, 'shop_name': f'示例店{shop}', 'name': f'20261111-103500_{number}',
        'stage': stage, 'supply_id': number, 'product_name': PRODUCT,
        'cross_border': False, 'order_ids': list(ids),
        'attempts': [
            {'step': 'create', 'result': 'accepted', 'http_status': 201},
            {'step': 'add', 'result': 'accepted', 'http_status': 204, 'order_ids': list(ids)},
            {'step': 'deliver', 'result': 'accepted', 'http_status': 204},
        ],
    }


def pending(platform_id, *, internal_id=None, **overrides):
    return dict(dict(
        id=str(internal_id or platform_id + 1000), platformOrderId=str(platform_id),
        shopIdText='fixture-shop-a', orderStatus='2', isNewOrder='1', tableBase='1',
        platformIdText='Wildberries', order_label='<div></div>', cansend1logisticsHtml='<span></span>',
        order_ellipsis_other_title=f'商品种类:1 数量:{VC}*1',
        order_ellipsis_title='商品种类:1 数量:fixture-sku*1', order_combosku_data='[]',
        logisticsId='', trackNumber='', transportTime='', deliver_real_time='--',
        orderDeliverTime='--', expressTime='--', timeCancel='--',
    ), **overrides)


class TestVoidWorkflow(TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.data = Path(stack.enter_context(TemporaryDirectory()))
        stack.enter_context(patch.object(paths, 'current', paths.DataPaths(self.data)))
        self.cred = {'shop_map': {'fixture-shop-a': '示例店1(1)', 'fixture-shop-b': '示例店2(2)'}}
        stack.enter_context(patch.object(void, 'get_mabang_cred', return_value=self.cred))
        self.pending = stack.enter_context(patch.object(void, 'fetch_pending_orders', return_value=[pending(101)]))
        self.catalog = stack.enter_context(patch.object(void, 'load_local_mapping',
                                      return_value=({VC: PRODUCT}, {}, {VC: SHIPPING_MODE_WB})))
        self.shops = stack.enter_context(patch.object(void.bcs, 'fetch_open_api_shops',
                                       return_value=[OpenAPIShop(i, f'示例店{i}', f'fixture-key-{i}') for i in (1, 2)]))
        self.wb = stack.enter_context(patch.object(void, 'WBFBSAPI', autospec=True))
        self.clients = [Mock(spec=WBFBSAPI), Mock(spec=WBFBSAPI)]
        self.wb.side_effect = lambda key: self.clients[int(key[-1]) - 1]
        for api in self.clients:
            api.supply_order_ids.return_value = {101, 102, 103, 201}
            api.order_statuses.side_effect = lambda ids: {
                oid: {'supplierStatus': 'complete', 'wbStatus': 'waiting'} for oid in ids
            }
        self.invoice = stack.enter_context(patch.object(void, 'invoiced_orders', return_value=set()))
        self.submit = stack.enter_context(patch.object(void, 'cancel_pending_order'))
        stack.enter_context(patch.object(void.time, 'sleep'))
        self.store(supply())

    def store(self, *records):
        fbs_shipments.save_journal(fbs_shipments.journal_path(), list(records))

    def execute(self, *options):
        args = build_parser().parse_args(['order', 'mabang-void-wb', *options])
        with redirect_stdout(StringIO()):
            code = void.run(args)
        artifact = Path(args.output['artifacts']['csv'])
        return code, args.output, artifact

    def test_exact_preview_lists_every_order_without_any_write(self):
        code, reply, artifact = self.execute('--supply-id', SUPPLY, '--json')
        self.assertEqual(code, 0)
        self.assertEqual(reply['counts'], {'READY': 1, 'SKIPPED': 1})
        self.assertEqual([r['platform_order_id'] for r in reply['rows']], [101, 102])
        self.assertEqual(artifact.read_bytes()[:3], bytes.fromhex('efbbbf'))
        with artifact.open(encoding='utf-8-sig', newline='') as f:
            records = list(csv.DictReader(f))
        self.assertEqual([r['mabang_order_id'] for r in records], ['1101', ''])
        self.assertNotIn('feishu', records[0])
        self.invoice.assert_called_once_with(self.cred, [1101])
        self.pending.assert_called_once_with(self.cred)
        self.submit.assert_not_called()
        self.assertFalse(mabang_voids.ledger_path().exists())

    def test_batch_uses_pending_intersection_and_shop_filter(self):
        self.store(supply(), supply((801,), number=OLD, stage='complete'),
                   supply((201,), shop=2, number=SECOND, stage='complete'))
        self.pending.return_value += [pending(201, shopIdText='fixture-shop-b')]
        code, reply, _ = self.execute('--shops', '2')
        self.assertEqual(code, 0)
        self.assertEqual([r['platform_order_id'] for r in reply['rows']], [201])
        self.clients[0].supply_order_ids.assert_not_called()
        self.clients[1].supply_order_ids.assert_called_once_with(SECOND)
        self.clients[1].supply_order_ids.reset_mock()
        _, reply, _ = self.execute()
        self.assertEqual([r['platform_order_id'] for r in reply['rows']], [101, 201])
        self.clients[0].supply_order_ids.assert_called_once_with(SUPPLY)
        self.clients[1].supply_order_ids.assert_called_once_with(SECOND)

    def test_supply_number_missing_conflict_or_unaccepted_never_calls_cancel(self):
        self.assertNotEqual(self.execute('--supply-id', OLD, '--apply')[0], 0)
        self.store(supply(), supply((201,), shop=2))
        self.assertNotEqual(self.execute('--supply-id', SUPPLY, '--apply')[0], 0)
        self.store(supply(stage='building'))
        _, reply, _ = self.execute('--supply-id', SUPPLY, '--apply')
        self.assertEqual(reply['counts'], {'BLOCKED': 2})
        self.pending.assert_not_called()
        self.wb.assert_not_called()
        self.submit.assert_not_called()

    def test_incomplete_acceptance_and_changed_identity_block(self):
        broken = supply()
        broken['attempts'][1]['order_ids'] = [102]
        self.store(broken)
        self.assertEqual(self.execute('--supply-id', SUPPLY, '--apply')[1]['counts'], {'BLOCKED': 2})
        self.store(supply())
        for rows, counts in (
                ([pending(101, shopIdText='fixture-shop-b')], {'SKIPPED': 2}),
                ([pending(101), pending(101, internal_id=1103)], {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, platformIdText='Other')], {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, orderStatus='3')], {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, order_label='<i title="已预报"></i>')], {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, cansend1logisticsHtml='<i class="logisticsChannelText"></i>')],
                 {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, deliver_real_time='2026-01-01')], {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, trackNumber=None)], {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, order_ellipsis_other_title=f'商品种类:1 数量:ambiguous {VC}*1')],
                 {'BLOCKED': 1, 'SKIPPED': 1}),
                ([pending(101, order_combosku_data='[{}]')], {'BLOCKED': 1, 'SKIPPED': 1})):
            with self.subTest(rows=rows):
                self.pending.return_value = rows
                _, reply, _ = self.execute('--supply-id', SUPPLY, '--apply')
                self.assertEqual(reply['counts'], counts)
        self.invoice.assert_not_called()
        self.wb.assert_not_called()
        self.submit.assert_not_called()

    def test_current_mapping_must_still_be_wb_shipping_and_same_product(self):
        for catalog in (({VC: PRODUCT}, {}, {VC: SHIPPING_MODE_MABANG}),
                        ({VC: '其他示例商品'}, {}, {VC: SHIPPING_MODE_WB}),
                        ({}, {}, {})):
            with self.subTest(catalog=catalog):
                self.catalog.return_value = catalog
                _, reply, _ = self.execute('--apply')
                self.assertEqual(reply['counts'], {'BLOCKED': 1})
        self.submit.assert_not_called()

    def test_wb_membership_status_and_shop_auth_are_required(self):
        self.store(supply(), supply((103,), number=OLD), supply((201,), shop=2, number=SECOND))
        self.pending.return_value += [pending(103), pending(201, shopIdText='fixture-shop-b')]
        client = self.clients[0]
        client.supply_order_ids.side_effect = [{101}, FBSRequestError('fixture', status=403)]
        _, reply, _ = self.execute('--apply')
        self.assertEqual(reply['counts'], {'BLOCKED': 2, 'ACCEPTED': 1})
        client.order_statuses.assert_not_called()
        self.submit.assert_called_once_with(self.cred, 1201)

    def test_wb_absent_or_cancelled_orders_do_not_enter_invoice_gate(self):
        self.pending.return_value += [pending(102)]
        client = self.clients[0]
        client.supply_order_ids.return_value = {101, 102}
        client.order_statuses.side_effect = None
        for statuses in (
                {101: {'supplierStatus': 'complete', 'wbStatus': 'canceled'}},
                {101: {'supplierStatus': 'complete', 'wbStatus': 'waiting'}},
                {101: {'supplierStatus': 'confirm', 'wbStatus': 'waiting'}}):
            with self.subTest(statuses=statuses):
                client.order_statuses.return_value = statuses
                if statuses[101]['wbStatus'] == 'waiting' and statuses[101]['supplierStatus'] == 'complete':
                    client.supply_order_ids.return_value = {102}
                _, reply, _ = self.execute('--apply')
                self.assertEqual(reply['counts'], {'BLOCKED': 2})
        self.invoice.assert_not_called()
        self.submit.assert_not_called()

    def test_rejection_unknown_and_success_are_durable_and_not_replayed(self):
        self.store(supply((101, 102, 103)))
        self.pending.return_value = [pending(oid) for oid in (101, 102, 103)]
        effects = [orders.CancelRejected('fixture'), requests.Timeout(), None]

        def submit(cred, oid):
            ledger = mabang_voids.load_attempts(mabang_voids.ledger_path())
            self.assertEqual(ledger[1, oid - 1000]['status'], 'SUBMITTING')
            self.assertEqual(ledger[1, oid - 1000]['mabang_order_id'], oid)
            effect = effects.pop(0)
            if effect:
                raise effect
        self.submit.side_effect = submit
        code, reply, _ = self.execute('--apply')
        self.assertEqual(code, 1)
        self.assertEqual(reply['counts'], {'FAILED': 1, 'UNKNOWN': 1, 'ACCEPTED': 1})
        self.assertEqual(self.invoice.call_args_list, [call(self.cred, [oid]) for oid in (1101, 1102, 1103)])
        self.pending.assert_called_once()
        self.submit.reset_mock()
        self.invoice.reset_mock()
        self.pending.return_value.remove(pending(102))
        _, replay, _ = self.execute('--apply')
        self.assertEqual(replay['counts'], {'FAILED': 1, 'UNKNOWN': 1, 'SKIPPED': 1})
        self.submit.assert_not_called()
        self.invoice.assert_not_called()

    def test_invoice_precheck_and_failed_pending_read_preserve_blocked_rows(self):
        self.pending.return_value += [pending(102)]
        self.invoice.side_effect = [{1101}, RuntimeError('fixture')]
        _, reply, _ = self.execute('--apply')
        self.assertEqual(reply['counts'], {'BLOCKED': 2})
        self.assertEqual([r['invoice_state'] for r in reply['rows']], ['已开票', '未知'])
        self.pending.side_effect = RuntimeError('fixture')
        _, reply, _ = self.execute('--supply-id', SUPPLY, '--apply')
        self.assertEqual(reply['counts'], {'BLOCKED': 2})
        self.submit.assert_not_called()

    def test_interrupt_keeps_submitting_record_for_the_next_run(self):
        self.submit.side_effect = KeyboardInterrupt
        code, reply, _ = self.execute('--apply')
        self.assertEqual(code, 130)
        self.assertEqual(reply['counts'], {'UNKNOWN': 1})
        self.assertEqual(mabang_voids.load_attempts(mabang_voids.ledger_path())[1, 101]['status'], 'SUBMITTING')
        self.submit.reset_mock()
        self.invoice.reset_mock()
        self.assertEqual(self.execute('--apply')[1]['counts'], {'UNKNOWN': 1})
        self.submit.assert_not_called()
        self.invoice.assert_not_called()

    def test_ledger_failure_prevents_submit_and_corrupt_ledger_blocks(self):
        with patch.object(mabang_voids, 'save_attempts', side_effect=OSError('fixture')):
            _, reply, _ = self.execute('--apply')
        self.assertEqual(reply['counts'], {'BLOCKED': 1})
        mabang_voids.ledger_path().write_text('{', encoding='utf8')
        _, reply, _ = self.execute('--supply-id', SUPPLY, '--apply')
        self.assertEqual(reply['counts'], {'BLOCKED': 2})
        self.submit.assert_not_called()


class TestVoidAPI(TestCase):
    @staticmethod
    def response(payload, status=200):
        result = requests.Response()
        result.status_code = status
        result.headers['Content-Type'] = 'text/html'
        result._content = payload
        return result

    def test_invoice_request_uses_repeat_keys_and_checks_returned_ids(self):
        with patch.object(orders, 'form_request', return_value=self.response(
                b'{"success":true,"orderIdList":["1102"]}')) as send:
            self.assertEqual(orders.invoiced_orders({}, [1101, 1102]), {1102})
        self.assertEqual(send.call_args.args[2], 'order.getDoCancelOrder')
        self.assertEqual(send.call_args.kwargs['data'],
                         [('orderIds[]', '1101'), ('orderIds[]', '1102'), ('tableBase', '1')])
        self.assertTrue(send.call_args.kwargs['read_only'])
        for payload in (b'{"success":true}', b'{"success":true,"orderIdList":[102]}',
                        b'{"success":"true","orderIdList":[]}', b'<html>login</html>'):
            with self.subTest(payload=payload), patch.object(orders, 'form_request',
                                                            return_value=self.response(payload)):
                with self.assertRaises(RuntimeError):
                    orders.invoiced_orders({}, [1101])

    def test_cancel_submits_once_with_type_two_and_accepts_html_json(self):
        with patch.object(orders, 'form_request',
                          return_value=self.response(b'{"success":true,"message":""}')) as send:
            orders.cancel_pending_order({}, 1101)
        send.assert_called_once_with({}, orders.WWW_BASE, 'order.doCancelOrders', read_only=False,
                                     data={'orderIds': '1101', 'type': '2', 'remark': 'WB发货群供货已受理'})
        for payload in (b'{"success":false}', b'{"success":1}', b'<html>error</html>'):
            with self.subTest(payload=payload), patch.object(orders, 'form_request',
                                                            return_value=self.response(payload)) as send:
                with self.assertRaises(RuntimeError):
                    orders.cancel_pending_order({}, 1101)
                send.assert_called_once()

    def test_wb_membership_and_status_endpoints_remain_read_only(self):
        api = WBFBSAPI('fixture')
        self.addCleanup(api.close)
        with patch.object(api, '_fbs', side_effect=[
                {'orderIds': [101]}, {'orders': [{'id': 101, 'supplierStatus': 'complete'}]}]) as read:
            self.assertEqual(api.supply_order_ids(SUPPLY), {101})
            self.assertEqual(set(api.order_statuses([101])), {101})
        self.assertEqual(read.call_args_list, [
            call('GET', f'/api/marketplace/v3/supplies/{SUPPLY}/order-ids'),
            call('POST', '/api/v3/orders/status', json={'orders': [101]}),
        ])

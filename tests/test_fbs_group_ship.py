from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.util import find_spec
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from sellerops.features.orders import fbs_group_ship, matching
from sellerops.features.orders.fbs_pdfs import check_pair, export_pair
from sellerops.integrations.bcs.client import OpenAPIShop
from sellerops.integrations.wildberries.fbs_api import FBSRequestError, WBFBSAPI
from sellerops.integrations.wildberries.open_api import OpenAPIResponseError
from sellerops.persistence.fbs_shipments import load_journal, pdf_paths, supply_names
from sellerops.persistence.price_book import GROUP_SHIPPING_MARKER
from sellerops.settings import paths


SHOP = OpenAPIShop(9352, '袁州1', 'synthetic')
SECOND = OpenAPIShop(9353, '袁州2', 'synthetic')
SUPPLY = 'WB-GI-1234567'
PRODUCT = '黑丝假发'
CATALOG = ({'BCS-HSJF-A': PRODUCT, 'BCS-HSJF-B': PRODUCT, 'BCS-OTHER-A': '其他商品'},
           {PRODUCT: {GROUP_SHIPPING_MARKER}, '其他商品': {'SKU123'}})


def order(order_id, **changes):
    return dict(dict(id=order_id, article='BCS-HSJF-A', warehouseId=567, deliveryType='fbs',
                     cargoType=1, crossBorderType=0, options={'isB2B': False},
                     requiredMeta=[], optionalMeta=[]), **changes)


def fake_api(orders):
    api = Mock(spec=WBFBSAPI)
    api.warehouses.return_value = [{'id': 567, 'warehouseId': 999, 'name': '莫斯科仓库'}]
    api.new_orders.return_value = orders
    api.create_supply.return_value = SUPPLY
    api.stickers.side_effect = lambda ids, **kwargs: {order_id: b'label' for order_id in ids}
    return api


class FBSWorkflowTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(TemporaryDirectory())
        self.root = Path(tmp)
        stack.enter_context(patch.multiple(paths, DATA_DIR=tmp, STATE_DIR=str(self.root / 'state'),
                                           LOG_DIR=str(self.root / 'logs')))
        stack.enter_context(patch('sys.stdout', new_callable=StringIO))
        stack.enter_context(patch.object(fbs_group_ship, '_catalog', return_value=CATALOG))
        self.access = stack.enter_context(patch.object(fbs_group_ship, 'select_accesses',
                                                      return_value=(None, [SHOP])))
        self.api = stack.enter_context(patch.object(fbs_group_ship, 'WBFBSAPI'))
        stack.enter_context(patch.object(fbs_group_ship, 'require_pdf_tools'))
        self.pdf = stack.enter_context(patch.object(fbs_group_ship, 'export_pair'))

    def run_workflow(self, *, apply=False, resume=False):
        return fbs_group_ship.run(SimpleNamespace(shops='', name='', resume=resume, apply=apply))

    def journal(self):
        return load_journal(fbs_group_ship.journal_path())

    def test_preview_filters_moscow_and_never_submits(self):
        api = self.api.return_value = fake_api([
            order(1), order(2, warehouseId=999), order(3, article='BCS-OTHER-A'),
        ])
        self.assertEqual(self.run_workflow(), 0)
        api.create_supply.assert_not_called()
        self.pdf.assert_not_called()
        self.assertFalse(fbs_group_ship.journal_path().exists())
        report = next((self.root / 'logs').glob('*.json')).read_text(encoding='utf8')
        self.assertIn('"order_ids": [\n        1', report)

    def test_multiple_vcs_and_batches_share_a_supply_but_shops_do_not(self):
        directory = self.root / 'fbs_shipments' / '20261111'
        directory.mkdir(parents=True)
        existing = directory / 'earlier.pdf'
        existing.write_bytes(b'untouched')
        first = fake_api([order(n, article='BCS-HSJF-B' if n % 2 else 'BCS-HSJF-A')
                          for n in range(1, 102)])
        second = fake_api([order(1)])
        self.access.return_value = (None, [SHOP, SECOND])
        self.api.side_effect = [first, second]
        with patch.object(fbs_group_ship, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 11, 3, tzinfo=timezone.utc)
            self.assertEqual(self.run_workflow(apply=True), 0)
        first.create_supply.assert_called_once()
        second.create_supply.assert_called_once()
        self.assertEqual([len(call.args[1]) for call in first.add_orders.call_args_list], [100, 1])
        self.assertTrue(all(call.args[0] == SUPPLY for call in first.add_orders.call_args_list))
        first.deliver.assert_called_once_with(SUPPLY)
        self.assertEqual([row['label_pages'] for row in self.journal()], [101, 1])
        qr, labels, _, data = self.pdf.call_args_list[0].args
        self.assertEqual(len(data), 101)
        self.assertEqual(qr.name, '袁州1(9352)_黑丝假发_101单_1234567_供货码.pdf')
        self.assertEqual(labels.name, '袁州1(9352)_黑丝假发_101单_1234567_标签.pdf')
        second_qr, second_labels, _, _ = self.pdf.call_args_list[1].args
        self.assertEqual({qr.parent, labels.parent, second_qr.parent, second_labels.parent}, {directory})
        self.assertNotEqual(qr.name, second_qr.name)
        self.assertEqual(existing.read_bytes(), b'untouched')

    def test_incompatible_or_missing_required_metadata_blocks_entire_group(self):
        api = self.api.return_value = fake_api([order(1), order(2, cargoType=2)])
        self.assertEqual(self.run_workflow(apply=True), 1)
        api.create_supply.assert_not_called()
        api.new_orders.return_value = [order(1), order(2, requiredMeta=['customsDeclaration'])]
        api.metadata.return_value = {2: {'metaDetails': [
            {'key': 'customsDeclaration', 'decision': 'required', 'value': None}]}}
        self.assertEqual(self.run_workflow(apply=True), 1)
        api.create_supply.assert_not_called()

    def test_partial_add_and_unknown_write_cannot_be_replayed(self):
        api = self.api.return_value = fake_api([order(n) for n in range(1, 102)])
        api.add_orders.side_effect = [None, FBSRequestError('响应未知', uncertain=True)]
        self.assertEqual(self.run_workflow(apply=True), 1)
        record = self.journal()[0]
        self.assertEqual([attempt['result'] for attempt in record['attempts']],
                         ['accepted', 'accepted', 'unknown'])
        api.deliver.assert_not_called()
        self.assertEqual(self.run_workflow(apply=True), 1)
        self.assertEqual(self.run_workflow(apply=True, resume=True), 1)
        api.create_supply.assert_called_once()
        self.assertEqual(api.add_orders.call_count, 2)
        self.pdf.assert_not_called()

    def test_delivered_resume_only_exports_with_original_name(self):
        api = self.api.return_value = fake_api([order(1), order(2)])
        api.stickers.side_effect = OpenAPIResponseError('标签缺失')
        self.assertEqual(self.run_workflow(apply=True), 1)
        first = self.journal()[0]
        self.assertEqual(first['stage'], 'delivered')
        self.pdf.assert_not_called()
        # A previous run can have published only the QR before the second save failed.
        qr_path = Path(first['files']['supply_qr'])
        qr_path.parent.mkdir(parents=True)
        qr_path.touch()
        api.stickers.side_effect = None
        api.stickers.return_value = {1: b'one', 2: b'two'}
        self.assertEqual(self.run_workflow(apply=True, resume=True), 0)
        record = self.journal()[0]
        self.assertEqual((record['stage'], record['name'], record['label_pages']),
                         ('complete', first['name'], 2))
        self.assertEqual(record['files'], first['files'])
        self.assertEqual(self.pdf.call_args.args[3], [b'one', b'two'])
        api.create_supply.assert_called_once()
        api.add_orders.assert_called_once()
        api.deliver.assert_called_once()
        self.assertEqual(self.run_workflow(apply=True, resume=True), 0)
        self.pdf.assert_called_once()

    def test_mabang_routes_the_marker_out_of_sku_matching(self):
        item = {'shop': '袁州1', 'kind': '1', 'vc': 'BCS-HSJF-A', 'matched_sku': ''}
        with patch.object(matching, 'parse_order', return_value=item):
            row = matching.classify([{}], CATALOG[0], {PRODUCT: GROUP_SHIPPING_MARKER},
                                    {'袁州1': '袁州1(9352)'})[0]
        self.assertEqual((row['status'], row['expected_sku']), ('GROUP_SHIPPING', ''))


class FBSContractTests(unittest.TestCase):
    def test_name_uses_singapore_date_and_differentiates_same_second(self):
        used = set()
        before = datetime(2026, 11, 10, 15, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 10, 16, 1, tzinfo=timezone.utc)
        original, first = supply_names(before, 9352, SHOP.name, PRODUCT, 2, used)
        self.assertEqual(first, '20261110-235900_袁州1(9352)_黑丝假发_2单')
        self.assertEqual(original, first)
        _, second = supply_names(before, 9352, SHOP.name, PRODUCT, 2, used)
        self.assertEqual(second, first + '_02')
        original, safe = supply_names(after, 9352, SHOP.name, '假发/黑色' * 20, 2, used)
        self.assertTrue(safe.startswith('20261111-000100_'))
        self.assertIn('/', original)
        self.assertNotIn('/', safe)
        self.assertLessEqual(len(safe), 128)
        for name, day in ((first, '20261110'), (safe, '20261111')):
            record = {'name': name, 'supply_id': SUPPLY, 'shop_id': 9352, 'shop_name': SHOP.name,
                      'product_name': PRODUCT, 'order_ids': [1, 2]}
            qr, labels = pdf_paths(record)
            self.assertEqual(qr.parent, Path(paths.DATA_DIR) / 'fbs_shipments' / day)
            self.assertEqual(labels.parent, qr.parent)
            other_qr, _ = pdf_paths(dict(record, supply_id='WB-GI-1234568'))
            self.assertNotEqual(qr.name, other_qr.name)

    def test_api_writes_once_to_china_and_rejects_partial_labels(self):
        api = WBFBSAPI('synthetic')
        with patch.object(api.session, 'request', side_effect=requests.Timeout) as request:
            with self.assertRaises(FBSRequestError) as error:
                api.add_orders(SUPPLY, [1, 2])
            self.assertTrue(error.exception.uncertain)
            request.assert_called_once()
            self.assertEqual(request.call_args.args,
                             ('PATCH', f'https://marketplace-api.wildberries.cn/api/marketplace/v3/supplies/{SUPPLY}/orders'))
            self.assertEqual(request.call_args.kwargs['json'], {'orders': [1, 2]})
        with patch.object(api, '_fbs', return_value={'stickers': [{'orderId': 1, 'file': 'eA=='}]}):
            with self.assertRaises(OpenAPIResponseError):
                api.stickers([1, 2], cross_border=False)
        api.close()

    @unittest.skipUnless(find_spec('pypdf') and find_spec('reportlab'), 'PDF extra not installed')
    def test_pdf_pair_is_published_only_with_expected_pages_and_sizes(self):
        from PIL import Image
        data = BytesIO()
        Image.new('RGB', (400, 300), 'white').save(data, format='PNG')
        png = data.getvalue()
        with TemporaryDirectory() as tmp:
            dest = Path(tmp)
            qr, labels = dest / '供货二维码.pdf', dest / '订单标签.pdf'
            with self.assertRaises(OSError):
                export_pair(qr, labels, png, [png, b'broken'], cross_border=False)
            self.assertEqual(list(dest.iterdir()), [])
            export_pair(qr, labels, png, [png, png], cross_border=False)
            check_pair(qr, labels, 2)

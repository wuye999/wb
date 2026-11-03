"""合并上架的有限等待与续办边界；使用虚拟时间和合成商品卡。"""
from contextlib import ExitStack, redirect_stdout
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

from sellerops.core.errors import AuthenticationError
from sellerops.features.listings import open_api_finish as finish
from sellerops.features.listings.open_api_common import OperationLog
from sellerops.features.listings.open_api_payloads import create_payload
from sellerops.integrations.wildberries.content_api import WBContentAPI
from sellerops.integrations.wildberries.open_api import WBOpenAPI
from sellerops.persistence import open_api_publish as store
from sellerops.settings import paths
from test_open_api_writes import card, VC


class TestMergedPublishWait(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = paths.DATA_DIR
        self.addCleanup(paths.configure, previous)
        paths.configure(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.now = 0.0
        self.sleeps = []
        self.stack.enter_context(patch.object(finish.time, 'monotonic', side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(finish.time, 'sleep', side_effect=self.sleep))
        self.state = {'version': 1, 'items': {}}
        self.log = OperationLog('publish-openapi', True)
        self.api = Mock(spec=WBContentAPI)
        self.api.save_media.return_value = {'accepted': True}
        self.api.set_stocks.return_value = {'accepted': True}

    def sleep(self, seconds):
        self.assertLessEqual(seconds, 30)
        self.sleeps.append(seconds)
        self.now += seconds

    def continuation(self, vc=VC, sid=2, api=None):
        payload = create_payload(card(vc=vc), 59)
        payload['variants'][0]['sizes'][0]['skus'] = ['new-code-' + vc]
        entry = {'shopId': sid, 'vc': vc, 'sourceNm': 101, 'status': 'accepted',
                 'warehouse': {'id': 40 + sid, 'name': '莫斯科仓库'}, 'card': payload,
                 'media': ['https://images.example/101.webp'], 'stock': 0,
                 'receipt': {'accepted': True}, 'stages': {}}
        self.state['items'][store.item_key(sid, vc)] = entry
        row = self.log.add(sid, vc, 'planned', cardReceipt=entry['receipt'])
        return finish.PublishContinuation(entry, row, api or self.api, entry['warehouse'])

    def generated(self, vc=VC):
        return card(202, vc=vc, chrt=22, barcode='new-code-' + vc)

    def run_wait(self, items, *, timeout=30, interval=15, apply=True):
        store.save_state(self.state)
        finish.wait_and_finish(items, self.state, self.log, timeout=timeout, interval=interval, apply=apply)

    def test_waits_for_missing_card_then_complete_spec_ids_and_stops_reading_after_assets(self):
        item = self.continuation()
        partial = self.generated()
        partial['sizes'] = []
        pages = iter([[], [partial], [self.generated()]])
        def query(**kwargs):
            self.api.save_media.assert_not_called()
            self.api.set_stocks.assert_not_called()
            self.assertEqual(kwargs, {'vendor_code': VC, 'max_pages': 10})
            return next(pages)
        self.api.cards.side_effect = query
        self.run_wait([item], timeout=45)
        self.assertEqual(self.sleeps, [15, 15])
        self.assertEqual(self.api.cards.call_count, 3)
        self.assertEqual(item.entry['status'], 'finished')
        self.assertEqual(item.entry['targetNm'], 202)
        self.api.save_media.assert_called_once_with(202, ['https://images.example/101.webp'])
        self.api.set_stocks.assert_called_once_with(42, [{'chrtId': 22, 'amount': 0}])
        self.assertEqual(self.log.finish(), 0)

    def test_timeout_preserves_accepted_creation_for_resume_without_recreating(self):
        item = self.continuation()
        self.api.cards.return_value = []
        self.run_wait([item])
        self.assertEqual(self.now, 30)
        self.assertEqual(item.row['status'], 'timeout')
        self.assertEqual(item.entry['status'], 'accepted')
        self.assertTrue(finish.can_continue(item.entry))
        self.api.save_media.assert_not_called()
        self.api.set_stocks.assert_not_called()
        self.assertEqual(self.log.finish(), 1)
        self.api.cards.return_value = [self.generated()]
        self.run_wait([item], timeout=0)
        self.assertEqual(item.entry['status'], 'finished')
        self.api.create_cards.assert_not_called()
        self.api.barcodes.assert_not_called()

    def test_ready_subset_finishes_after_other_item_times_out_and_overall_is_nonzero(self):
        first, second = self.continuation(), self.continuation('OTHER-VC')
        def query(**kwargs):
            self.api.save_media.assert_not_called()
            return [self.generated()] if kwargs['vendor_code'] == VC else []
        self.api.cards.side_effect = query
        self.run_wait([first, second])
        self.assertEqual((first.entry['status'], second.entry['status']), ('finished', 'accepted'))
        self.assertEqual(second.row['status'], 'timeout')
        self.api.save_media.assert_called_once()
        self.api.set_stocks.assert_called_once()
        self.assertEqual(self.log.finish(), 1)

    def test_read_failure_stops_whole_shop_but_other_shop_can_finish(self):
        first, second = self.continuation(), self.continuation('SECOND-VC')
        other_api = Mock(spec=WBContentAPI)
        other_api.cards.return_value = [self.generated('THIRD-VC')]
        other_api.save_media.return_value = other_api.set_stocks.return_value = {'accepted': True}
        third = self.continuation('THIRD-VC', sid=3, api=other_api)
        self.api.cards.side_effect = [[self.generated()], AuthenticationError('fixture-secret')]
        self.run_wait([first, second, third], timeout=0)
        self.assertEqual(first.row['status'], 'blocked')
        self.assertEqual(second.row['status'], 'failed')
        self.assertEqual(third.entry['status'], 'finished')
        self.api.save_media.assert_not_called()
        other_api.set_stocks.assert_called_once_with(43, [{'chrtId': 22, 'amount': 0}])
        self.assertNotIn('fixture-secret', self.log.path.read_text(encoding='utf8'))

    def test_resume_preserves_accepted_media_and_original_stock_without_repeating_media(self):
        item = self.continuation()
        item.entry['stages']['media'] = {'status': 'accepted', 'receipt': {'accepted': True}}
        target = self.generated()
        target['video'] = 'https://images.example/keep.m3u8'
        self.api.cards.return_value = [target]
        self.run_wait([item])
        self.api.save_media.assert_not_called()
        self.api.set_stocks.assert_called_once_with(42, [{'chrtId': 22, 'amount': 0}])
        self.assertEqual(item.entry['status'], 'finished')

    def test_preview_does_one_probe_without_sleep_writes_or_state_change(self):
        item = self.continuation()
        self.api.cards.return_value = []
        store.save_state(self.state)
        before = store.state_path().read_bytes()
        self.run_wait([item], apply=False)
        self.assertEqual(self.api.cards.call_count, 1)
        self.assertFalse(self.sleeps)
        self.assertEqual(store.state_path().read_bytes(), before)
        self.api.save_media.assert_not_called()
        self.api.set_stocks.assert_not_called()

    def test_fuzzy_search_other_vc_cannot_receive_media_or_stock(self):
        item = self.continuation()
        self.api.cards.return_value = [self.generated(VC + '-SIMILAR')]
        self.run_wait([item], timeout=0)
        self.assertEqual(item.row['status'], 'timeout')
        self.api.save_media.assert_not_called()
        self.api.set_stocks.assert_not_called()

    def test_round_cap_terminates_even_if_monotonic_clock_does_not_advance(self):
        item = self.continuation()
        self.api.cards.return_value = []
        with patch.object(finish.time, 'sleep'):
            self.run_wait([item], timeout=2, interval=1)
        self.assertLessEqual(self.api.cards.call_count, 3)
        self.assertEqual(item.row['status'], 'timeout')

    def test_invalid_wait_options_fail_before_accessing_credentials(self):
        from sellerops.cli.main import main
        with patch('sellerops.features.listings.open_api_publish.select_accesses') as accesses:
            for option, value in (('--wait-timeout', '-1'), ('--wait-timeout', 'nan'),
                                  ('--wait-timeout', '7201'), ('--wait-interval', '0'),
                                  ('--wait-interval', 'inf')):
                self.assertEqual(main(['publish-openapi', '--shops', '2', '--nm', '101', '--vc', VC,
                                       option, value]), 1)
            accesses.assert_not_called()

    def test_vendor_code_search_preserves_pagination_and_includes_without_photo(self):
        api = WBOpenAPI('fixture-secret', 'cn')
        self.addCleanup(api.close)
        with patch.object(api, '_request', return_value={'cards': [], 'cursor': {'total': 0}}) as request:
            self.assertEqual(api.cards(vendor_code=VC, max_pages=10), [])
        self.assertEqual(request.call_args.kwargs['json']['settings']['filter'],
                         {'withPhoto': -1, 'textSearch': VC})


if __name__ == '__main__':
    unittest.main()

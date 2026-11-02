"""Failure propagation and submission boundaries with synthetic platform responses."""
from contextlib import ExitStack, redirect_stdout
import io
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SHOP = {'shopId': 42, 'shopName': 'fixture'}


def credential_fixture():
    cred = Mock(root_version='fixture', ai={}, ai_key='fixture',
                ai_base_url='https://fixture.invalid', ai_model='fixture')
    cred.wb_shops.return_value = [SHOP]
    cred.wb_shop.return_value = SHOP
    return cred


class TestCommandFailures(unittest.TestCase):
    def setUp(self):
        from sellerops.cli.main import build_parser
        self.parser = build_parser()
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_shop_and_item_failures_propagate_in_preview_and_apply(self):
        from sellerops.features.listings import banned
        from sellerops.features.pricing import promo, price_review
        from sellerops.features.support import questions
        cases = [
            (banned, 'banned', {'total': 1, 'moved': 0, 'failed': 1}),
            (promo, 'promo-apply', {'joinable': 1, 'applied': 0, 'skipped': 0, 'failed': 1}),
            (price_review, 'price-review', {'total': 1, 'applied': 0, 'failed': 1}),
            (questions, 'questions', {'unanswered': 1, 'replied': 0, 'failed': 1}),
        ]
        for module, command, stats in cases:
            for error in [RuntimeError('fixture query failed'), module.common.CookieExpiredError('expired'), None]:
                for apply in [False, True]:
                    with self.subTest(command=command, error=error, apply=apply), ExitStack() as stack:
                        args = self.parser.parse_args([command, *(['--apply'] if apply else [])])
                        args.yes = True
                        stack.enter_context(patch.object(module.credentials, 'get', return_value=credential_fixture()))
                        stack.enter_context(patch.object(module.time, 'sleep'))
                        stack.enter_context(patch.object(module, 'process_shop', return_value=stats, side_effect=error))
                        if module is banned:
                            stack.enter_context(patch.object(module.wb_api, 'make_session'))
                        if module is questions:
                            stack.enter_context(patch.object(module.MappingRepository, 'build_vc_resolver', return_value=(Mock(), {})))
                            stack.enter_context(patch.object(module.MappingRepository, 'load_mapping_state', return_value=({}, {})))
                        self.assertEqual(module.run(args), 1)

    def test_banned_rejecting_confirmation_prevents_all_platform_requests(self):
        from sellerops.features.listings import banned
        args = self.parser.parse_args(['banned', '--apply'])
        with patch.object(banned.credentials, 'get', return_value=credential_fixture()), \
                patch('builtins.input', return_value='n') as prompt, \
                patch.object(banned.wb_api, 'make_session') as session, \
                patch.object(banned, 'process_shop') as process:
            self.assertEqual(banned.run(args), 0)
        prompt.assert_called_once()
        session.assert_not_called()
        process.assert_not_called()

    def test_price_stock_and_bcs_operations_return_partial_failure(self):
        from sellerops.features.listings import prices, stocks, operations
        plan = [{'shopId': 42, 'items': [{'nmID': 123, 'vc': 'BCS-TEST-123',
                 'price': 10, 'chrtId': 456, 'amount': 1}]}]
        for module, command, planner, executor in [
            (prices, 'price', 'plan_price', 'exec_shop'),
            (stocks, 'stock', 'build_plans', 'exec_online'),
            (operations, 'price-bcs', 'plan_price', 'run_apply'),
        ]:
            with self.subTest(command=command), ExitStack() as stack:
                args = self.parser.parse_args([command, '--apply', '--yes'])
                stack.enter_context(patch.object(module, 'resolve_filters', return_value=(['BCS-TEST-123'], 'fixture')))
                stack.enter_context(patch.object(module, 'load_state', return_value=({}, {}, [])))
                stack.enter_context(patch.object(module, planner, return_value=plan))
                stack.enter_context(patch.object(module, executor, return_value=(1, 1)))
                if module is operations:
                    stack.enter_context(patch.object(module, 'get_shops', return_value=[42]))
                    result = module.run('price', args)
                else:
                    stack.enter_context(patch.object(module, '_filter_shops', return_value=[42]))
                    stack.enter_context(patch.object(module.credentials, 'get', return_value=credential_fixture()))
                    stack.enter_context(patch.object(module.wb_api, 'make_session'))
                    stack.enter_context(patch.object(module, '_append_csv'))
                    stack.enter_context(patch.object(module, '_report_zero_items'))
                    result = module.run(args)
                self.assertEqual(result, 1)

    def test_clean_all_preserves_failures_from_first_target(self):
        from sellerops.features.listings import clean
        args = self.parser.parse_args(['clean', '--target', 'all', '--apply'])
        for first_error in [None, RuntimeError('draft query failed')]:
            with self.subTest(error=first_error), \
                    patch.object(clean.credentials, 'get', return_value=credential_fixture()), \
                    patch.object(clean.bcs, 'fetch_shop_list', return_value=[{'id': 42, 'name': 'fixture'}]), \
                    patch.object(clean.wb_api, 'make_session'), patch.object(clean.time, 'sleep'), \
                    patch.object(clean, 'process_draft', side_effect=first_error,
                                 return_value={'total': 1, 'deleted': 0, 'failed': 1, 'zeroed': 0}), \
                    patch.object(clean, 'process_basket',
                                 return_value={'total': 0, 'deleted': 0, 'failed': 0, 'zeroed': 0}) as basket:
                self.assertEqual(clean.run(args), 1)
                basket.assert_called_once()

    def test_discount_bcs_does_not_report_empty_after_failed_collection(self):
        from sellerops.features.pricing import discount_bcs
        args = self.parser.parse_args(['discount-bcs'])
        with patch.object(discount_bcs.bcs, 'fetch_shop_list', return_value=[{'id': 42, 'name': 'fixture'}]), \
                patch.object(discount_bcs, 'collect', side_effect=RuntimeError('expired')), \
                patch.object(discount_bcs.time, 'sleep'):
            self.assertEqual(discount_bcs.run(args), 1)

    def test_discount_bcs_missing_item_response_is_failure(self):
        from sellerops.features.pricing import discount_bcs
        args = self.parser.parse_args(['discount-bcs', '--apply'])
        item = {'nmID': 123, 'vc': 'BCS-TEST-123', 'title': 'fixture', 'old_d': 50, 'price': 100}
        with patch.object(discount_bcs.bcs, 'fetch_shop_list', return_value=[{'id': 42, 'name': 'fixture'}]), \
                patch.object(discount_bcs, 'collect', return_value=([item], {'skip_no_price': 0})), \
                patch.object(discount_bcs, 'apply', return_value={}), \
                patch.object(discount_bcs, 'write_csv', return_value='fixture.csv'), \
                patch.object(discount_bcs.time, 'sleep'):
            self.assertEqual(discount_bcs.run(args), 1)

    def test_publish_partial_success_and_existing_only_results(self):
        from sellerops.features.listings import bulk_publish, card_publish
        from sellerops.domain.models import PublishItem
        item = PublishItem(nm_id=123, price=10, prefix='BCS-TEST')
        for module, command in [(bulk_publish, 'publish'), (card_publish, 'create-cards')]:
            for counts, expected in [({'ok': 1, 'fail': 1}, 1), ({'ok': 0, 'fail': 0, 'skip': 1}, 0)]:
                with self.subTest(command=command, counts=counts), \
                        patch.object(module.MappingRepository, 'load_mapping_state', return_value=({}, {})), \
                        patch.object(module.MappingRepository, 'load_prefix_map', return_value={}), \
                        patch.object(module, 'price_book_packages', return_value={}), \
                        patch.object(module, 'resolve_publish_details', return_value=(item, None)), \
                        patch.object(module, 'execute_publish', return_value=counts):
                    self.assertEqual(module.run(self.parser.parse_args([command, '123', '--apply'])), expected)

    def test_daily_stops_before_discount_after_failed_promo(self):
        from sellerops.automation import daily
        with tempfile.TemporaryDirectory() as directory, patch.object(daily.config, 'LOG_DIR', directory), \
                patch.object(daily.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='')) as run:
            self.assertEqual(daily.run_daily_cmd(self.parser.parse_args(['daily', 'morning', '--apply'])), 1)
            run.assert_called_once()
            self.assertIn('promo-apply', run.call_args.args[0])

    def test_read_reports_propagate_failed_shop_queries(self):
        from sellerops.features.support import complaints, chat_inspect
        from sellerops.features.pricing import adverts
        for module, command in [(complaints, 'appeals'), (chat_inspect, 'chats'), (adverts, 'promo-goods')]:
            with self.subTest(command=command), ExitStack() as stack:
                stack.enter_context(patch.object(module.credentials, 'get', return_value=credential_fixture()))
                stack.enter_context(patch.object(module.time, 'sleep'))
                if module is adverts:
                    stack.enter_context(patch.object(module, 'process_shop', side_effect=RuntimeError('query failed')))
                else:
                    stack.enter_context(patch.object(module.wb_api, 'make_session', side_effect=RuntimeError('query failed')))
                    stack.enter_context(patch.object(module, '_LocalResolver' if module is complaints else 'NmResolver'))
                self.assertEqual(module.run(self.parser.parse_args([command])), 1)

    def test_dimension_partial_failure_is_nonzero(self):
        from sellerops.features.listings import dimension
        args = self.parser.parse_args(['dimension', '--vc', 'BCS-TEST-123', '--dims', '1*2*3/0.5', '--apply'])
        with tempfile.TemporaryDirectory() as directory, patch.object(dimension.config, 'LOG_DIR', directory), \
                patch.object(dimension.ops, 'load_state', return_value=({}, {}, [])), \
                patch.object(dimension.ops, 'get_shops', return_value=[42]), \
                patch.object(dimension, '_resolve_filters', return_value=['BCS-TEST-123']), \
                patch.object(dimension, 'build_plans', return_value=[{}]), \
                patch.object(dimension, 'apply_plans', return_value=(1, 1, [])):
            self.assertEqual(dimension.run(args), 1)

    def test_ai_test_empty_generation_is_failure(self):
        import json
        from sellerops.features.support import ai_reply_test
        with tempfile.TemporaryDirectory() as directory:
            qa = directory + '/qa.json'
            with open(qa, 'w', encoding='utf8') as file:
                json.dump([{'question': 'fixture', 'product': {}}], file)
            with patch.object(ai_reply_test.config, 'LOG_DIR', directory), \
                    patch.object(ai_reply_test.credentials, 'get', return_value=credential_fixture()), \
                    patch.object(ai_reply_test, 'LLMClient') as client:
                client.return_value.generate_reply.return_value = None
                self.assertEqual(ai_reply_test.run(SimpleNamespace(qa=qa)), 1)


class TestWatchFailures(unittest.TestCase):
    def test_poll_failure_surfaces_in_both_watchers(self):
        from sellerops.features.support import questions_watch, chat_watch
        args = SimpleNamespace(shops='', mode='front', apply=False)
        for module in [questions_watch, chat_watch]:
            with self.subTest(module=module.__name__), redirect_stdout(io.StringIO()), \
                    patch.object(module.credentials, 'get', return_value=credential_fixture()), \
                    patch.object(module.wb_api, 'make_session', side_effect=RuntimeError('fixture unavailable')), \
                    patch.object(module.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, '本轮 1 项失败'):
                    module.poll_once(args, {}, None, set(), {}, None, None)

    def test_question_send_failure_is_not_logged_as_preview_or_remembered_as_success(self):
        from sellerops.features.support import questions_watch as watcher
        args = SimpleNamespace(shops='', mode='back', apply=True)
        writer, replied, client = Mock(), set(), Mock()
        client.generate_reply.return_value = 'fixture reply'
        with redirect_stdout(io.StringIO()), \
                patch.object(watcher.credentials, 'get', return_value=credential_fixture()), \
                patch.object(watcher.wb_api, 'make_session'), \
                patch.object(watcher.questions, 'fetch_questions', return_value=[{'id': 'q1'}]), \
                patch.object(watcher.questions, 'reply', return_value={'error': True, 'errorText': 'failed'}), \
                patch.object(watcher, 'product_info_str', return_value='fixture'), \
                patch.object(watcher, 'save_replied') as save, patch.object(watcher.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                watcher.poll_once(args, {}, client, replied, set(), writer, None)
        self.assertEqual(writer.writerow.call_args.args[0][-1], '提交失败')
        self.assertEqual(replied, set())
        save.assert_not_called()


class TestWarehouseAndParameters(unittest.TestCase):
    def test_warehouse_clear_reads_once_and_propagates_auth_failure(self):
        from sellerops.features.listings import warehouse_clear as warehouse
        args = SimpleNamespace(apply=True, interval=0)
        for expired in [False, True]:
            results = []
            with self.subTest(expired=expired), redirect_stdout(io.StringIO()), \
                    patch.object(warehouse.wb_api, 'make_session', side_effect=warehouse.common.CookieExpiredError('expired') if expired else None), \
                    patch.object(warehouse, 'fetch_stocks', return_value=[{'chrtId': 456}]) as fetch, \
                    patch.object(warehouse, 'delete_stock', return_value=(True, '')) as delete, \
                    patch.object(warehouse.time, 'sleep'):
                warehouse.process_shop(SHOP, 100, credential_fixture(), args, results)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1]['shop_errors'], int(expired))
            self.assertEqual(fetch.call_count, 0 if expired else 1)
            self.assertEqual(delete.call_count, 0 if expired else 1)

    def test_invalid_batch_options_fail_before_any_platform_request(self):
        from sellerops.cli.main import build_parser
        from sellerops.features.listings import prices, stocks, operations, bulk_publish, card_publish
        parser = build_parser()
        for command, module in [('price', prices), ('stock', stocks), ('trash', operations)]:
            for option, value in [('--chunk', '0'), ('--chunk', '-1'), ('--interval', '-1'), ('--interval', 'nan')]:
                with self.subTest(command=command, option=option, value=value), \
                        patch.object(module, 'resolve_filters') as resolve:
                    args = parser.parse_args([command, '--apply', '--yes', option, value])
                    with self.assertRaises(ValueError):
                        module.run('trash', args) if module is operations else module.run(args)
                    resolve.assert_not_called()
        for module in [bulk_publish, card_publish]:
            with patch.object(module.bcs, 'fetch_shop_list') as fetch:
                with self.assertRaises(ValueError):
                    module.execute_publish([], apply=True, interval=-1)
                fetch.assert_not_called()
        for module, function, arguments in [(prices, prices.apply_prices, (SHOP, [])),
                                              (stocks, stocks.set_stock, (SHOP, 100, []))]:
            with patch.object(module.wb_api, 'make_session') as session:
                with self.assertRaises(ValueError):
                    function(*arguments, interval=-1)
                session.assert_not_called()


class TestOrderFailures(unittest.TestCase):
    def test_feishu_creation_requires_complete_unique_record_ids(self):
        from sellerops.features.orders import feishu_register as register
        for response in [{}, {'record_id_list': ['r1']}, {'record_id_list': ['r1', 'r1']}]:
            with self.subTest(response=response), patch.object(register, 'invoke', return_value=response):
                with self.assertRaisesRegex(RuntimeError, '创建响应不完整'):
                    register.batch_create('fixture', 'fixture', [{}, {}])
        with patch.object(register, 'invoke', return_value={'record_id_list': ['r1', 'r2']}):
            self.assertEqual(register.batch_create('fixture', 'fixture', [{}, {}]), 2)

    def test_pipeline_registration_failures_and_replace_failures_are_nonzero(self):
        from sellerops.cli.main import build_parser
        from sellerops.features.orders import mabang_process as pipeline
        parser = build_parser()
        for replace_failed in [False, True]:
            for registration in [0, 1, RuntimeError('lark-cli failed')]:
                def match(args, **kwargs):
                    args.replace_fail_order_ids = ['failed-order'] if replace_failed else []
                    return 0
                with self.subTest(replace_failed=replace_failed, registration=registration), redirect_stdout(io.StringIO()), \
                        patch.object(pipeline, 'mabang_run', side_effect=match), \
                        patch.object(pipeline, 'mabang_run_forecast', return_value=0) as forecast, \
                        patch.object(pipeline, 'feishu_register_run', return_value=registration,
                                     side_effect=registration if isinstance(registration, Exception) else None):
                    code = pipeline.run(parser.parse_args(['mabang-process', '--url', 'https://fixture.invalid', '--apply']))
                    self.assertEqual(code, int(replace_failed or registration != 0))
                    self.assertEqual(forecast.call_args.args[0].replace_fail_order_ids,
                                     ['failed-order'] if replace_failed else [])

    def test_forecast_does_not_handover_when_one_failed_batch_has_unknown_shop(self):
        from sellerops.features.orders import forecast
        target = [{'order_id': 'order', 'platform_order_id': 'platform', 'shop': 'unrelated-shop',
                   'has_forecast': False, 'channel_selected': False}]
        args = SimpleNamespace(apply=True, check=False, wait=0, upload_timeout=1, poll_interval=1)
        result = {'rows': {}, 'store_of': {'B1': 'known-shop'}, 'success': set(),
                  'failed': {'B1', 'B2'}, 'still': set()}
        with redirect_stdout(io.StringIO()), \
                patch.object(forecast, 'mb_client_get_mabang_cred', return_value={}), \
                patch.object(forecast, 'collect_forecast_orders', return_value=(target, {})), \
                patch.object(forecast, 'mb_client_get_forecast_logistics', return_value=('logistics', 'channel')), \
                patch.object(forecast, 'mb_client_batch_create_forecast', return_value=(['B1', 'B2'], 'created')), \
                patch.object(forecast, 'mb_client_upload_forecast_batch', return_value='uploaded'), \
                patch.object(forecast, 'wait_upload_done', return_value=result), \
                patch.object(forecast, 'find_batches', return_value={'found': {}}), \
                patch.object(forecast, 'mb_client_set_handover') as handover, \
                patch.object(forecast, 'write_forecast_csv', return_value='fixture.csv'):
            self.assertEqual(forecast.run_forecast(args), 1)
        self.assertEqual(target[0]['_step_handover'], '跳过-预报失败')
        handover.assert_not_called()

    def test_forecast_handover_failure_and_empty_creation_are_nonzero(self):
        from sellerops.features.orders import forecast
        args = SimpleNamespace(apply=True, check=False, wait=0, upload_timeout=1, poll_interval=1)
        for has_forecast in [False, True]:
            target = [{'order_id': 'order', 'platform_order_id': 'platform', 'shop': 'fixture',
                       'has_forecast': has_forecast, 'channel_selected': False}]
            with self.subTest(has_forecast=has_forecast), redirect_stdout(io.StringIO()), \
                    patch.object(forecast, 'mb_client_get_mabang_cred', return_value={}), \
                    patch.object(forecast, 'collect_forecast_orders', return_value=(target, {})), \
                    patch.object(forecast, 'mb_client_get_forecast_logistics', return_value=('logistics', 'channel')), \
                    patch.object(forecast, 'mb_client_batch_create_forecast', return_value=([], 'missing')), \
                    patch.object(forecast, 'mb_client_get_handover_channel_value', return_value='fixture'), \
                    patch.object(forecast, 'mb_client_set_handover', return_value=([], [])) as handover, \
                    patch.object(forecast, 'write_forecast_csv', return_value='fixture.csv'):
                if has_forecast:
                    self.assertEqual(forecast.run_forecast(args), 1)
                else:
                    with self.assertRaisesRegex(RuntimeError, '未返回批次号'):
                        forecast.run_forecast(args)
                    handover.assert_not_called()

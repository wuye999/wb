"""临时BCS账号的离线解析、隔离、合并及不落盘边界。"""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from openpyxl import load_workbook

from sellerops.cli.main import build_parser
from sellerops.domain.product_reports import ReportCatalog
from sellerops.core.errors import AuthenticationError, PlatformApiError, RateLimitError
from sellerops.features.catalog import product_report as workflow
from sellerops.features.catalog.product_report_analysis import analyze
from sellerops.integrations.bcs.capture import BCS_BASE_URL, BCSSession, MAX_CAPTURE_CHARS, parse_sessions
from sellerops.integrations.bcs.client import BCSClient, OpenAPIShop
from sellerops.settings import credentials
from test_product_report import NOW, VC, feedback, order, register


def capture(token='temporary-token-a', key='temporary-limit-a', *, url=None, headers=None):
    headers = headers if headers is not None else {
        'authorization': 'Bearer ' + token, 'x-limit-key': key,
        'cookie': f'Admin-Token={token}; Limit-Key={key}; gateway=temporary-cookie-a'}
    return 'fetch(' + json.dumps(url or BCS_BASE_URL + '/shopKeeper/productList/list?shopId=-1') + ', ' + json.dumps({
        'headers': headers, 'method': 'GET', 'body': None}, ensure_ascii=False) + ');'


class TestBCSCapture(unittest.TestCase):
    def test_one_or_multiple_fetch_blocks_and_markdown(self):
        first = capture()
        second = capture('temporary-token-b', 'temporary-limit-b')
        sessions = parse_sessions('\ufeff```csharp\n' + first + '\n```\n```js\n' + second + '\n```')
        self.assertEqual([item.token for item in sessions], ['temporary-token-a', 'temporary-token-b'])
        self.assertEqual(len(parse_sessions(first)), 1)
        for secret in ('temporary-token-a', 'temporary-limit-a', 'temporary-cookie-a'):
            self.assertNotIn(secret, repr(sessions))

    def test_headers_case_insensitive_and_independent(self):
        session = parse_sessions(capture(headers={
            'Authorization': 'bearer token', 'X-LIMIT-KEY': 'limit', 'Cookie': 'gateway=cookie'}))[0]
        headers = session.headers()
        self.assertEqual(headers['Authorization'], 'Bearer token')
        headers['Cookie'] = 'changed'
        self.assertEqual(session.headers()['Cookie'], 'gateway=cookie')

    def test_empty_malformed_executable_or_oversized_input_is_safe(self):
        for text in ('', '```js\n```', 'fetch("temporary-token-a", {broken)',
                     capture() + '\nprocess.exit();', 'x' * (MAX_CAPTURE_CHARS + 1)):
            with self.subTest(text_length=len(text)), self.assertRaises(ValueError) as error:
                parse_sessions(text)
            self.assertNotIn('temporary-token-a', str(error.exception))

    def test_only_fixed_bcs_https_domain_is_accepted(self):
        for url in ('https://example.invalid/prod-api/list', 'http://wb.bcserp.com/prod-api/list',
                    'https://wb.bcserp.com.evil.invalid/prod-api/list',
                    'https://token@wb.bcserp.com/prod-api/list',
                    'https://wb.bcserp.com:444/prod-api/list', 'https://wb.bcserp.com/other/list'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_sessions(capture(url=url))

    def test_required_headers_duplicates_and_control_characters(self):
        base = {'authorization': 'Bearer token', 'x-limit-key': 'limit', 'cookie': 'gateway=cookie'}
        cases = [{key: value for key, value in base.items() if key != missing} for missing in base]
        cases.extend([{**base, 'Authorization': 'Bearer second-token'},
                      {**base, 'cookie': 'gateway=secret\r\nInjected: true'},
                      {**base, 'authorization': 'Basic token'},
                      {**base, 'x-limit-key': 123}])
        for headers in cases:
            with self.subTest(keys=list(headers)), self.assertRaises(ValueError):
                parse_sessions(capture(headers=headers))

    def test_mixed_cookie_credentials_are_rejected_without_leaking(self):
        for cookie in ('Admin-Token=other-secret; Limit-Key=limit',
                       'Admin-Token=token; Limit-Key=other-secret',
                       'Admin-Token=token; Admin-Token=token'):
            with self.assertRaises(ValueError) as error:
                parse_sessions(capture(headers={
                    'authorization': 'Bearer token', 'x-limit-key': 'limit', 'cookie': cookie}))
            self.assertNotIn('other-secret', str(error.exception))

    def test_duplicate_json_fields_are_rejected_instead_of_overwriting_credentials(self):
        text = capture().replace('"headers": {', '"headers": {"authorization": "Bearer other-secret", ', 1)
        with self.assertRaises(ValueError) as error:
            parse_sessions(text)
        self.assertNotIn('other-secret', str(error.exception))
        self.assertNotIn('temporary-token-a', str(error.exception))

    def test_temporary_http_clients_do_not_read_or_replace_local_credentials(self):
        sessions = parse_sessions(capture() + '\n' + capture('temporary-token-b', 'temporary-limit-b'))
        response = Mock(status_code=200)
        response.json.return_value = {'code': 200, 'rows': [], 'total': 0}
        with patch.object(credentials, 'get', side_effect=AssertionError('must not read local credentials')), \
                patch('sellerops.integrations.bcs.client.requests.request', return_value=response) as request:
            for session in sessions:
                self.assertEqual(BCSClient(session).fetch_open_api_shops(), [])
        calls = request.call_args_list
        self.assertEqual([call.kwargs['headers']['Authorization'] for call in calls],
                         ['Bearer temporary-token-a', 'Bearer temporary-token-b'])
        self.assertTrue(all(call.args[1].startswith(BCS_BASE_URL + '/system/wbShop/user/list?') for call in calls))
        self.assertNotIn('shopKeeper', calls[0].args[1])  # 抓包地址不会被重放。

    def test_default_client_is_lazy_and_uses_existing_local_credentials(self):
        with patch.object(credentials, 'get') as get:
            client = BCSClient()
            get.assert_not_called()
            get.return_value.base_url = 'local-base'
            get.return_value.bcs_headers.return_value = {'Authorization': 'local'}
            self.assertEqual(client.base_url, 'local-base')
            self.assertEqual(client._headers(), {'Authorization': 'local'})


class TestTemporaryReportAccounts(unittest.TestCase):
    def fixture(self, accounts=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(workflow, 'load_report_catalog', return_value=ReportCatalog({'测试商品'}, {VC: '测试商品'})))
        self.local = stack.enter_context(patch.object(workflow.bcs, 'fetch_open_api_shops', return_value=[
            OpenAPIShop(11, '店甲', 'official-11'), OpenAPIShop(12, '店乙', 'official-12')]))
        accounts = accounts if accounts is not None else [[OpenAPIShop(21, '外部店', 'official-21')]]
        clients = []
        for accesses in accounts:
            client = Mock()
            if isinstance(accesses, Exception):
                client.fetch_open_api_shops.side_effect = accesses
            else:
                client.fetch_open_api_shops.return_value = accesses
            clients.append(client)
        self.clients = clients
        self.bcs_factory = stack.enter_context(patch.object(workflow, 'BCSClient', side_effect=clients))

        def snapshot(sid):
            if sid not in (11, 12):
                raise AssertionError('temporary shops must not read local snapshots')
            return {'shopId': sid, 'fetchedAt': '2026-11-01', 'rows': [{'nmId': sid * 10, 'vendorCode': VC}]}

        self.snapshots = stack.enter_context(patch.object(
            workflow.ProductSnapshotRepository, 'load_snapshot_document', side_effect=snapshot))
        self.records = stack.enter_context(patch.object(workflow, 'load_records', return_value={
            VC: {'11': 'local-pending', '21': 'must-not-use-temporary-pending'}}))
        stack.enter_context(patch.object(workflow.MappingRepository, 'build_vc_resolver',
                                         return_value=(lambda vc, fallback='': '测试商品' if vc else fallback, {})))
        stack.enter_context(patch.object(workflow.tables, 'resolve_base', return_value='base'))
        stack.enter_context(patch.object(workflow.tables, 'resolve_table', return_value='table'))
        self.feishu = stack.enter_context(patch.object(workflow.tables, 'list_records', return_value=[
            register(nm=110), register(nm=210, shop='外部店', rid='external')]))
        self.apis = {}
        for sid in (11, 12, 21, 22, 31):
            api = Mock()
            api.warehouses.return_value = [{'name': '莫斯科仓库', 'id': sid * 100, 'warehouseId': 999999}]
            api.feedbacks.return_value = [feedback(nm=sid * 10)]
            api.orders.return_value = [order(nm=sid * 10, status='return' if sid >= 20 else 'buyout')]
            api.fbs_orders.return_value = [{'rid': 'o1', 'warehouseId': sid * 100, 'nmId': sid * 10, 'article': VC}]
            self.apis['official-' + str(sid)] = api
        self.api_factory = stack.enter_context(patch.object(
            workflow, 'WBOpenAPI', side_effect=lambda key, region: self.apis[key]))
        return [BCSSession('temporary-token-' + str(index), 'temporary-limit-' + str(index),
                           'gateway=temporary-cookie-' + str(index)) for index in range(len(accounts))]

    def collect(self, sessions, **kwargs):
        return workflow.collect(now=NOW, url='fixture', extra_bcs_accounts=sessions, **kwargs)

    def test_combines_statistics_but_keeps_only_local_gap_targets(self):
        sessions = self.fixture()
        report = self.collect(sessions)
        self.assertFalse(report.failed)
        self.assertEqual(report.target_shop_ids, {11, 12})
        self.assertEqual({shop['id'] for shop in report.shops}, {11, 12, 21})
        self.assertEqual(len(report.orders), 3)
        self.assertEqual(len(report.feedbacks), 3)  # 相同评价ID在不同店铺不误去重。
        self.assertEqual(report.coverage[21]['snapshot'], '不适用：仅补充统计')
        self.assertEqual([call.args[0] for call in self.snapshots.call_args_list], [11, 12])
        self.assertNotIn('21', report.publish_records[VC])
        result = analyze(report)
        self.assertEqual({row['shop_id'] for row in result['gaps']}, {11, 12})
        self.assertEqual(result['by_vc'][0]['return_rate'], 1 / 3)
        self.assertEqual(result['vc_feedback'][0]['negative'], 3)
        external = next(row for row in result['links'] if row['shop_id'] == 21)
        self.assertEqual(external['negative'], 1)
        self.assertIn('飞书出单记录（历史）', external['source'])
        self.feishu.assert_called_once()
        self.assertTrue(all(self.apis[key].close.called for key in ('official-11', 'official-12', 'official-21')))

    def test_shops_filters_only_local_targets_and_multiple_accounts_are_supported(self):
        sessions = self.fixture([[OpenAPIShop(21, '外部店', 'official-21')],
                                 [OpenAPIShop(31, '第三店', 'official-31')]])
        report = self.collect(sessions, shops='11')
        self.assertFalse(report.failed)
        self.assertEqual(report.target_shop_ids, {11})
        self.assertEqual({shop['id'] for shop in report.shops}, {11, 21, 31})
        self.assertEqual({row['shop_id'] for row in analyze(report)['gaps']}, {11})
        self.assertEqual([shop['account_source'] for shop in report.shops], ['本机账号', '临时账号1', '临时账号2'])
        self.apis['official-12'].orders.assert_not_called()

    def test_duplicate_ids_use_local_then_first_temporary_source(self):
        sessions = self.fixture([
            [OpenAPIShop(11, '店甲', 'wrong-local-key'), OpenAPIShop(21, '外部店', 'official-21')],
            [OpenAPIShop(21, '外部店', 'wrong-extra-key'), OpenAPIShop(31, '第三店', 'official-31')]])
        report = self.collect(sessions)
        self.assertFalse(report.failed)
        self.assertEqual(len(report.orders), 4)
        self.assertEqual([call.args[0] for call in self.api_factory.call_args_list],
                         ['official-11', 'official-12', 'official-21', 'official-31'])

    def test_duplicate_name_conflict_is_an_error_without_replacing_the_source(self):
        sessions = self.fixture([[OpenAPIShop(11, '冲突店名', 'wrong-key'), OpenAPIShop(21, '外部店', 'official-21')]])
        report = self.collect(sessions)
        self.assertTrue(report.failed)
        self.assertTrue(any(issue.shop_id == 11 and '名称冲突' in issue.message for issue in report.issues))
        self.assertEqual(len(report.orders), 3)
        self.assertEqual(next(shop['name'] for shop in report.shops if shop['id'] == 11), '店甲')

    def test_temporary_id_in_shops_is_not_promoted_to_a_local_target(self):
        sessions = self.fixture()
        report = self.collect(sessions, shops='21')
        self.assertTrue(report.failed)
        self.assertEqual(report.target_shop_ids, set())
        self.assertEqual([shop['id'] for shop in report.shops], [21])
        self.assertFalse(analyze(report)['gaps'])
        self.snapshots.assert_not_called()
        self.records.assert_not_called()

    def test_account_auth_or_pagination_failure_does_not_cancel_healthy_sources(self):
        for error in (AuthenticationError('temporary-token-0'), PlatformApiError('temporary-cookie-0')):
            with self.subTest(error=type(error).__name__):
                sessions = self.fixture([error, [OpenAPIShop(31, '第三店', 'official-31')]])
                report = self.collect(sessions)
                self.assertTrue(report.failed)
                self.assertEqual({row['shop_id'] for row in report.orders}, {11, 12, 31})
                self.assertTrue(any(issue.source == 'BCS/临时账号1' for issue in report.issues))
                self.assertEqual(report.unavailable_accounts, ['临时账号1'])
                result = analyze(report)
                for key in ('by_cn', 'by_vc', 'vc_feedback'):
                    self.assertTrue(all('账号店铺范围未知：临时账号1' in row['scope'] for row in result[key]))
                self.assertNotIn('temporary-token-0', repr(report))
                self.assertNotIn('temporary-cookie-0', repr(report))

    def test_local_account_failure_still_collects_temporary_sources(self):
        sessions = self.fixture()
        self.local.side_effect = AuthenticationError('local-secret')
        report = self.collect(sessions)
        self.assertTrue(report.failed)
        self.assertEqual(report.target_shop_ids, set())
        self.assertEqual({row['shop_id'] for row in report.orders}, {21})
        self.assertIn('账号店铺范围未知：本机账号', analyze(report)['by_vc'][0]['scope'])
        self.snapshots.assert_not_called()
        self.records.assert_not_called()

    def test_temporary_shop_rate_limit_and_missing_moscow_remain_unknown(self):
        for bad_warehouse in (False, True):
            with self.subTest(bad_warehouse=bad_warehouse):
                sessions = self.fixture()
                api = self.apis['official-21']
                if bad_warehouse:
                    api.warehouses.return_value = [{'id': 1, 'name': '成都仓库'}]
                else:
                    api.orders.side_effect = RateLimitError('temporary-token-0')
                report = self.collect(sessions)
                self.assertTrue(report.failed)
                external = next(row for row in analyze(report)['links'] if row['shop_id'] == 21)
                self.assertIsNone(external['orders'])
                if bad_warehouse:
                    self.assertIsNone(external['negative'])
                    api.orders.assert_not_called()
                    api.feedbacks.assert_not_called()
                self.assertEqual(report.coverage[11]['orders'], '完整')

    def test_cli_stdin_utf8_no_secret_output_or_credential_files(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / 'credentials.json'
            sentinel.write_bytes(b'{"local":"unchanged"}')
            output = root / 'report.xlsx'
            args = build_parser().parse_args(['product-report', '--shops', '11', '--extra-bcs-stdin', '--details',
                                              '--url', 'fixture', '--out', str(output)])
            # UTF-8管道不受Windows文本流默认编码影响。
            stdin = io.TextIOWrapper(io.BytesIO(capture().encode('utf-8')), encoding='gbk')
            self.addCleanup(stdin.close)
            stdout = io.StringIO()
            with patch.object(workflow.sys, 'stdin', stdin), redirect_stdout(stdout):
                self.assertEqual(workflow.run(args), 0)
            self.assertEqual(sentinel.read_bytes(), b'{"local":"unchanged"}')
            self.assertEqual({path.name for path in root.iterdir()}, {'credentials.json', 'report.xlsx'})
            with ZipFile(output) as workbook:
                xml = b'\n'.join(workbook.read(name) for name in workbook.namelist())
            for secret in ('temporary-token-a', 'temporary-limit-a', 'temporary-cookie-a', 'official-21'):
                self.assertNotIn(secret, stdout.getvalue())
                self.assertNotIn(secret.encode(), xml)
            wb = load_workbook(output)
            self.addCleanup(wb.close)
            rows = list(wb['店铺数据覆盖'].values)
            source_col, role_col = rows[0].index('账号来源'), rows[0].index('店铺用途')
            extra = next(row for row in rows[1:] if row[0] == 21)
            self.assertEqual((extra[source_col], extra[role_col]), ('临时账号1', '仅补充统计'))
            self.assertEqual({row[0] for row in list(wb['店铺链接缺口'].values)[1:]}, {11})

    def test_invalid_or_missing_stdin_fails_before_any_collection(self):
        args = SimpleNamespace(extra_bcs_stdin=True, out='')
        for text in ('', capture() + 'unexpected trailing code', capture(headers={})):
            with patch.object(workflow.sys, 'stdin', io.StringIO(text)), \
                    patch.object(workflow, 'collect') as collect, self.assertRaises(ValueError) as error:
                workflow.run(args)
            collect.assert_not_called()
            self.assertNotIn('temporary-token-a', str(error.exception))

    def test_stdin_terminal_encoding_and_size_errors_fail_before_collection(self):
        args = SimpleNamespace(extra_bcs_stdin=True, out='')
        terminal = Mock()
        terminal.isatty.return_value = True
        streams = [terminal, io.TextIOWrapper(io.BytesIO(b'\xff'), encoding='utf-8'),
                   io.StringIO('x' * (MAX_CAPTURE_CHARS + 1))]
        for stdin in streams:
            with patch.object(workflow.sys, 'stdin', stdin), patch.object(workflow, 'collect') as collect, \
                    self.assertRaises(ValueError):
                workflow.run(args)
            collect.assert_not_called()
        terminal.read.assert_not_called()
        streams[1].close()

    def test_failed_account_is_visible_in_saved_report_without_secrets(self):
        self.fixture([AuthenticationError('temporary-token-a; temporary-cookie-a')])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.xlsx'
            args = build_parser().parse_args(['product-report', '--extra-bcs-stdin', '--details',
                                              '--url', 'fixture', '--out', str(output)])
            stdout = io.StringIO()
            with patch.object(workflow.sys, 'stdin', io.StringIO(capture())), redirect_stdout(stdout):
                self.assertEqual(workflow.run(args), 1)
            wb = load_workbook(output)
            self.addCleanup(wb.close)
            self.assertEqual(dict(wb['数据覆盖说明'].values)['店铺范围未知的账号'], '临时账号1')
            with ZipFile(output) as workbook:
                xml = b'\n'.join(workbook.read(name) for name in workbook.namelist())
            self.assertIn('账号店铺范围未知：临时账号1'.encode(), xml)
            for secret in ('temporary-token-a', 'temporary-limit-a', 'temporary-cookie-a'):
                self.assertNotIn(secret, stdout.getvalue())
                self.assertNotIn(secret.encode(), xml)

    def test_without_flag_never_reads_stdin_or_constructs_extra_clients(self):
        self.fixture([])
        stdin = Mock()
        stdin.read.side_effect = AssertionError('must not read stdin')
        report = workflow.collect(now=NOW, url='fixture')
        with tempfile.TemporaryDirectory() as directory, patch.object(workflow.sys, 'stdin', stdin), \
                patch.object(workflow, 'collect', return_value=report):
            args = build_parser().parse_args(['product-report', '--out', str(Path(directory) / 'report.xlsx')])
            self.assertEqual(workflow.run(args), 0)
        self.bcs_factory.assert_not_called()
        self.assertEqual(report.target_shop_ids, {11, 12})
        stdin.read.assert_not_called()

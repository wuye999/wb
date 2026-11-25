"""The default scope is all new pending orders in configured shops, with complete reads."""
import contextlib
import io
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from sellerops.cli.main import build_parser
from sellerops.features.orders import matching
from sellerops.integrations.mabang import orders, warehouses
from sellerops.persistence.price_book import SHIPPING_MODE_MABANG


class TestPendingOrderScope(unittest.TestCase):
    def test_cli_defaults_are_unlimited_and_explicit_days_remain_available(self):
        parser = build_parser()
        for command in ['process', 'match', 'forecast', 'register']:
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args(["order", command]).days, 0)
                self.assertEqual(parser.parse_args(["order", command, '--days', '7']).days, 7)
        self.assertEqual(parser.parse_args(['order', 'list']).days, 0)

    def fetch(self, pages, **kwargs):
        responses = [Mock(json=Mock(return_value={'success':True,'orderDataList':rows})) for rows in pages]
        with patch.object(orders,'form_request',side_effect=responses) as post, \
                patch.object(orders.time,'sleep'), contextlib.redirect_stdout(io.StringIO()):
            result = orders.fetch_pending_orders({'www_cookie':'fixture'}, **kwargs)
        return result, [parse_qs(call.kwargs['data'], keep_blank_values=True) for call in post.call_args_list]

    def test_default_sends_empty_dates_keeps_new_pending_filters_and_reads_every_page(self):
        result, forms = self.fetch([[{'id':1},{'id':2}],[{'id':3}]],page_size=2)
        self.assertEqual([r['id'] for r in result],[1,2,3])
        for index,form in enumerate(forms,1):
            self.assertEqual(form['startTime1'],[''])
            self.assertEqual(form['endTime1'],[''])
            self.assertEqual(form['OrderPlus.isNewOrder'],['1'])
            self.assertEqual(form['Order.orderStatus'],['99'])
            self.assertEqual(form['tabId'],['7'])
            self.assertEqual(form['page'],[str(index)])

    def test_explicit_days_sets_a_real_date_window(self):
        _, forms = self.fetch([[]],days=7)
        fmt = '%Y-%m-%d %H:%M:%S'
        start = datetime.strptime(forms[0]['startTime1'][0],fmt)
        end = datetime.strptime(forms[0]['endTime1'][0],fmt)
        self.assertEqual(end-start,timedelta(days=7))

    def test_bad_days_fail_before_network(self):
        for days in [-1,True,1.5]:
            with self.subTest(days=days), patch.object(orders,'form_request') as post, self.assertRaises(ValueError):
                orders.fetch_pending_orders({'www_cookie':'fixture'},days=days)
            post.assert_not_called()

    def test_page_limit_and_repeated_pages_fail_instead_of_returning_partial_orders(self):
        with self.assertRaisesRegex(RuntimeError,'结果不完整'):
            self.fetch([[{'id':1},{'id':2}]],page_size=2,max_pages=1)
        with self.assertRaisesRegex(RuntimeError,'分页重复'):
            self.fetch([[{'id':1},{'id':2}],[{'id':1},{'id':2}]],page_size=2)

    def test_reported_pending_pagination_detects_truncation(self):
        response = Mock(json=Mock(return_value={'success': True, 'page': 1, 'rowsPerPage': 100,
                                                'pageCount': 2, 'orderDataList': [{'id': 1}]}))
        with patch.object(orders, 'form_request', return_value=response), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '总单数不符'):
            orders.fetch_pending_orders({'www_cookie': 'fixture'})

    def test_missing_order_list_is_not_reported_as_empty(self):
        response = Mock(json=Mock(return_value={'success':True}))
        with patch.object(orders,'form_request',return_value=response), self.assertRaisesRegex(RuntimeError,'有效订单列表'):
            orders.fetch_pending_orders({'www_cookie':'fixture'})

    def test_all_dates_still_only_allow_configured_shops(self):
        def row(oid,shop):
            return {'id':oid,'shopIdText':shop,'order_ellipsis_other_title':'数量:BCS-AAAA-123',
                    'order_ellipsis_title':'商品种类:1 数量:old*1'}
        classified = matching.classify([row(1,'configured'),row(2,'unrelated')],
            {'BCS-AAAA-123':'fixture'}, {'BCS-AAAA-123':'SKU'},
            {'BCS-AAAA-123': SHIPPING_MODE_MABANG}, {'configured':'configured(1)'})
        self.assertEqual([r['status'] for r in classified],['REPLACE','SKIP_SHOP'])

    def test_match_uses_mabang_warehouse_configuration_in_messages_and_writes(self):
        order = {'id': 1, 'shopIdText': 'configured', 'order_ellipsis_other_title': '数量:BCS-AAAA-123'}
        vc = 'BCS-AAAA-123'
        for warehouse in (None, '42'):
            output = io.StringIO()
            cred = {'shop_map': {'configured': '测试店(1)'}, 'warehouse_id': warehouse}
            with self.subTest(warehouse=warehouse), contextlib.redirect_stdout(output), \
                    patch.object(matching, 'get_mabang_cred', return_value=cred), \
                    patch.object(matching, 'fetch_pending_orders', return_value=[order]), \
                    patch.object(matching, 'load_local_mapping',
                                 return_value=({vc: '测试品'}, {vc: 'SKU'}, {vc: SHIPPING_MODE_MABANG})), \
                    patch.object(matching, 'fetch_enabled_warehouses', return_value=[]) as warehouse_list, \
                    patch.object(matching, 'api_ready', return_value=True), patch.object(matching, 'write_csv'), \
                    patch.object(matching, 'apply_replace', return_value=(1, 0)) as replace:
                result = matching.match_orders(days=1, page_size=100, apply=True)
            self.assertIn('mabang.warehouse_id', output.getvalue())
            self.assertEqual(warehouse_list.call_count, int(warehouse is None))
            self.assertNotIn('莫斯科', output.getvalue())
            self.assertEqual(result.stopped, warehouse is None)
            if warehouse is None:
                replace.assert_not_called()
            else:
                self.assertEqual(replace.call_args.args[2], 42)


    def test_keyword_resolution_is_shared_by_preview_and_apply(self):
        row = {'id': 1, 'shopIdText': 'fixture-shop', 'order_ellipsis_other_title': '数量:BCS-AAAA-123'}
        vc = 'BCS-AAAA-123'
        catalog = ({vc: '示例商品'}, {vc: 'fixture-sku'}, {vc: SHIPPING_MODE_MABANG})
        cred = {'shop_map': {'fixture-shop': '示例(1)'}, 'warehouse_keyword': '专用'}
        available = [{'id': 12, 'name': '备用仓'}, {'id': 13, 'name': '专用仓'}]
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(matching, 'get_mabang_cred', return_value=cred), \
                patch.object(matching, 'fetch_pending_orders', return_value=[row]), \
                patch.object(matching, 'load_local_mapping', return_value=catalog), \
                patch.object(matching, 'fetch_enabled_warehouses', return_value=available) as read, \
                patch.object(matching, 'api_ready', return_value=True), \
                patch.object(matching, 'write_csv'), \
                patch.object(matching, 'apply_replace', return_value=(1, 0)) as replace:
            self.assertFalse(matching.match_orders(days=0, page_size=100, apply=False).stopped)
            replace.assert_not_called()
            self.assertFalse(matching.match_orders(days=0, page_size=100, apply=True).stopped)
            self.assertEqual(replace.call_args.args[2], 13)
            self.assertEqual(read.call_count, 2)
            cred['warehouse_id'] = '12'
            self.assertTrue(matching.match_orders(days=0, page_size=100, apply=True).stopped)
            replace.assert_called_once()
            cred['warehouse_id'] = None
            for keyword in ('', '缺失', '用'):
                cred['warehouse_keyword'] = keyword
                result = matching.match_orders(days=0, page_size=100, apply=True)
                self.assertTrue(result.stopped)
            replace.assert_called_once()


class TestWarehouseList(unittest.TestCase):
    @staticmethod
    def page(current, rows, *, size=2, pages=2, total=3):
        return Mock(json=Mock(return_value={'code': 200, 'success': True, 'data': {
            'current': current, 'pages': pages, 'total': total, 'size': size, 'records': rows,
        }}))

    def test_paginates_without_category_filter_and_uses_only_enabled(self):
        pages = [
            self.page(1, [{'id': 1, 'name': ' 停用仓 ', 'status': 2},
                          {'id': 2, 'name': ' 启用仓 ', 'status': 1}]),
            self.page(2, [{'id': 3, 'name': '示例仓', 'status': 1}]),
        ]
        with patch.object(warehouses, 'post_warehouse_list', side_effect=pages) as post, \
                patch.object(warehouses.time, 'sleep'):
            self.assertEqual(warehouses.fetch_enabled_warehouses({}), [
                {'id': 2, 'name': '启用仓'}, {'id': 3, 'name': '示例仓'},
            ])
        self.assertEqual([c.args[1] for c in post.call_args_list], [
            {'orderBy': 'asc', 'status': 1, 'pageIndex': n, 'pageSize': 100} for n in (1, 2)
        ])

    def test_invalid_pagination_and_rows_fail_closed(self):
        good = {'id': 2, 'name': '启用仓', 'status': 1}
        first = self.page(1, [dict(good, id=1), good])
        cases = [
            (first, self.page(1, [dict(good, id=3)])),
            (first, self.page(2, [], total=2, pages=1)),
            (first, self.page(2, [good])),
            (first, self.page(2, [dict(good, id=3, status=3)])),
            (first, self.page(2, [dict(good, id='3')])),
            (first, self.page(2, [dict(good, id=3, name=' ')])),
            (self.page(1, [good, dict(good, id=3)], total=201, pages=101),),
        ]
        for pages in cases:
            with self.subTest(pages=pages), patch.object(warehouses.time, 'sleep'), \
                    patch.object(warehouses, 'post_warehouse_list', side_effect=pages):
                with self.assertRaises((ValueError, RuntimeError)):
                    warehouses.fetch_enabled_warehouses({})

    def test_accepts_empty_directory(self):
        with patch.object(warehouses, 'post_warehouse_list', return_value=self.page(1, [], pages=0, total=0)):
            self.assertEqual(warehouses.fetch_enabled_warehouses({}), [])

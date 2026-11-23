"""通过正式命令和脱敏平台回执检查商品选择、提交边界与数据隔离。"""

from catalog_fixture import map_vcs, corrupt_snapshot
import csv
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import openpyxl
import requests

from sellerops.cli.main import build_parser
from operation_fixture import run_business_cli as main
from sellerops.core.errors import AuthenticationError, PlatformApiError
from sellerops.features.products import clean, warehouse
from sellerops.integrations.wildberries import client as wb
from sellerops.persistence.snapshots import ProductSnapshotRepository as Snapshots
from sellerops.settings import paths


def response(body, status=200):
    return Mock(status_code=status, json=Mock(return_value=body))


def trash_card(nm, stocks):
    return {"nmId": nm, "vendorCode": f"VC-{nm}", "sizeList": [
        {"chrtId": chrt, "stockList": [{"warehouseId": wid, "amount": n} for wid, n in by_wh.items()]}
        for chrt, by_wh in stocks]}


class ProductBoundaries(unittest.TestCase):
    def test_scope_and_amount_must_be_explicit(self):
        parser = build_parser()
        for tail in (["--name", "杯", "--amount", "0"], ["--shops", "1", "--amount", "0"],
                     ["--shops", "1", "--all-products"], ["--shops", "1,", "--vc", "x", "--amount", "5"],
                     ["--all-shops", "--all-products", "--amount", "5", "--app"],
                     ["--all-shops", "--all-products", "--name", "杯", "--amount", "5"]):
            with self.subTest(tail=tail), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["product", "stock", *tail])
        self.assertEqual(parser.parse_args(["product", "stock", "--all-shops", "--all-products",
                                           "--amount", "0"]).amount, 0)

    def test_explicit_display_mode_cannot_send_messages(self):
        from sellerops.features.support import chat_watch, questions_watch
        parser = build_parser()
        for command, module in [("watch-chats", chat_watch), ("watch-questions", questions_watch)]:
            args = parser.parse_args(["support", command, "--mode", "front", "--apply"])
            with self.subTest(command=command), patch.object(module.credentials, "get") as cred:
                with self.assertRaisesRegex(ValueError, "展示模式"):
                    module.run(args)
                cred.assert_not_called()

    def test_receipt_only_accepts_unambiguous_requested_ids(self):
        cases = [
            ({"error": False, "data": {"nmIDs": [1]}, "additionalErrors":
              {"errors": [{"params": {"1": "2"}}]}}, {1}),
            ({"error": False, "data": {"nmIDs": [1, 1, 2]}}, {2}),
            ({"error": False, "data": {"nmIDs": [1, 3]}}, set()),
            ({"error": False, "data": {"nmIDs": [1]}, "additionalErrors": {"unrecognized": "x"}}, set()),
            ({"error": True, "data": {"nmIDs": [1]}}, set()),
        ]
        for receipt, accepted in cases:
            with self.subTest(receipt=receipt), patch.object(wb, "request_post", return_value=receipt) as post:
                states = clean.delete_nms(object(), [1, 2])
                self.assertEqual({nm for nm, s in states.items() if s == "删除已受理"}, accepted)
                self.assertEqual(post.call_args.args[2], {"nmIDs": [1, 2]})

    def basket(self, rows, apply, limit, zero):
        output = []
        with patch.object(warehouse, "resolve_warehouse", return_value=44), \
                patch.object(clean.bcs, "fetch_shop_products", return_value=rows) as fetch, \
                patch.object(clean.bcs, "post", return_value=zero) as post, \
                patch.object(wb, "request_post", side_effect=lambda s, url, p, **kw:
                             {"error": False, "data": {"nmIDs": p["nmIDs"]}}) as delete, redirect_stdout(StringIO()):
            result = clean.process_basket({"shopName": "模拟"}, 7, object(),
                                          SimpleNamespace(apply=apply, limit=limit), output)
            fetch.assert_called_once_with(7, "TRASH")
        return result, post, delete, output

    def test_basket_limit_all_sizes_and_preview_have_identical_scope(self):
        cards = [trash_card(1, [(11, {44: 3}), (12, {44: 0})]),
                 trash_card(2, [(21, {44: 0, 55: 99})]), trash_card(3, [(31, {44: 1})])]
        _, post, delete, preview = self.basket(cards, False, 2, {"code": 200})
        post.assert_not_called()
        delete.assert_not_called()
        result, post, delete, written = self.basket(cards, True, 2, {"code": 200})
        self.assertEqual([r["ID"] for r in preview], [r["ID"] for r in written])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(post.call_args.args[1], {"shopId": 7, "warehouses": [
            {"warehouseId": 44, "stockItems": [{"chrtId": 11, "amount": 0}, {"chrtId": 12, "amount": 0}]}]})
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(delete.call_args.args[2], {"nmIDs": [1, 2]})

    def test_basket_missing_spec_unknown_moscow_or_failed_zero_prevents_delete(self):
        cards = [trash_card(1, [(11, {44: 2})]), trash_card(2, [(21, {55: 9})]), trash_card(3, [])]
        result, post, delete, _ = self.basket(cards, True, 0, {"code": 500})
        self.assertEqual(result["failed"], 3)
        self.assertEqual(post.call_count, 1)
        delete.assert_not_called()

    def test_bcs_discount_blocks_ambiguous_prices_and_keeps_unknown_discount_out_of_threshold(self):
        from sellerops.features.products.discount_bcs import collect
        rows = [{"vendorCode": "a", "nmId": 1, "sizeList": [{"price": 10}], "discount": None},
                {"vendorCode": "b", "nmId": 2, "sizeList": [{"price": 10}, {"price": 20}], "discount": 70},
                {"vendorCode": "c", "nmId": 3, "sizeList": [{"price": 30}], "discount": 70}]
        with patch.object(clean.bcs, "fetch_shop_products", return_value=rows):
            plan = collect(7, 50, 0, 35)
            self.assertEqual([row["nmID"] for row in plan["items"]], [3])
            self.assertEqual([row["vc"] for row in plan["blocked_items"]], ["a", "b"])
            plan = collect(7, -1, 0, 35)
            self.assertEqual([row["nmID"] for row in plan["items"]], [1, 3])

    def test_storewide_discounts_skip_excluded_vcs_in_both_channels(self):
        from sellerops.features.products import discount_bcs, discounts

        entries = {'VC-1': {'status': 'excluded'}}
        args = SimpleNamespace(target=30, limit=0, threshold=-1, below=-1)
        wb_client = Mock()
        wb_client.fetch_discount_goods_desc.return_value = [
            {'nmID': 1, 'vendorCode': 'VC-1', 'discount': 40},
            {'nmID': 2, 'vendorCode': 'VC-2', 'discount': 40}]
        wb_plan = discounts._storewide_plan(7, wb_client, args, lambda vc, default: default, entries)
        rows = [dict(row, nmId=row['nmID'], sizeList=[{'price': 20}])
                for row in wb_client.fetch_discount_goods_desc.return_value]
        with patch.object(discount_bcs.bcs, 'fetch_shop_products', return_value=rows):
            bcs_plan = discount_bcs.collect(7, -1, 0, 30, entries=entries)
        for plan in (wb_plan, bcs_plan):
            self.assertEqual([row['vc'] for row in plan['items']], ['VC-2'])
            self.assertEqual([row['vc'] for row in plan['skipped_items']], ['VC-1'])

    def test_write_timeout_is_not_replayed_and_read_post_can_retry(self):
        session = Mock()
        session.request.side_effect = [requests.Timeout(), response({"items": []})]
        with patch.object(wb.time, "sleep"), self.assertRaises(PlatformApiError):
            wb.request_post(session, "https://fixture.invalid", {})
        self.assertEqual(session.request.call_count, 1)
        session.request.side_effect = [requests.Timeout(), response({"items": []})]
        with patch.object(wb.time, "sleep"):
            self.assertEqual(wb.request_post(session, "https://fixture.invalid", {}, read_only=True), {"items": []})
        self.assertEqual(session.request.call_count, 3)

    def test_ambiguous_precheck_never_confirms_and_auth_stops_session(self):
        session = Mock()
        with patch.object(wb, "make_session", return_value=session):
            client = wb.WBClient({"shopId": 7})
        for body in ({}, {"error": False, "data": None}):
            session.request.reset_mock()
            session.request.return_value = response(body)
            self.assertFalse(client.upload_batch_discount([{"nmID": 10}]).success)
            self.assertEqual(session.request.call_count, 1)
        session.request.reset_mock()
        session.request.return_value = response({}, 403)
        for _ in range(2):
            with self.assertRaises(AuthenticationError):
                client.upload_batch_discount([{"nmID": 10}])
        self.assertEqual(session.request.call_count, 1)


class OperationalExamples(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.callback(paths.configure, paths.current.data_dir)
        self.folder = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(redirect_stdout(StringIO()))
        paths.configure(self.folder)
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Sheet1"
        sheet.append(["卖家SKU", "产品中文名", "最低售价", "vendorCode前缀码"])
        sheet.append(["CUP", "旅行杯", 129, "AAAA"])
        sheet.append(["COVER", "旅行杯套", 20, "BBBB"])
        book.save(paths.current.price_book_xlsx)
        book.close()
        self.shops = [{"shopId": sid, "shopName": f"模拟{sid}", "authorizev3": "test",
                       "wb_seller_lk": str(sid)} for sid in (1, 2)]
        cred = Mock(shops=self.shops, root_version="offline", wb_warehouse_keyword="莫斯科")
        cred.wb_shop.side_effect = lambda sid: self.shops[sid - 1]
        self.stack.enter_context(patch.object(wb.credentials, "get", return_value=cred))
        self.stack.enter_context(patch.object(warehouse, "list_stores",
                                 side_effect=lambda sid: [{"id": 30 + sid, "name": "莫斯科", "warehouseId": 999}]))
        self.stack.enter_context(patch.object(wb.time, "sleep"))
        for sid in (1, 2):
            Snapshots.save_shop_products(sid, [{"vendorCode": vc, "nmId": sid * 100 + n,
                "sizeList": [{"chrtId": sid * 1000 + n}]} for n, vc in enumerate(
                ["BCS-AAAA-1", "BCS-AAAA-2", "BCS-BBBB-3"], 1)])

        map_vcs({'BCS-AAAA-1': 'CUP', 'BCS-AAAA-2': 'CUP', 'BCS-BBBB-3': 'COVER'}, source='prefix')

    def run_command(self, *args):
        return main(["--data-dir", str(self.folder), "product", *args])

    def test_price_and_discount_use_exact_name_and_shop_local_ids(self):
        with patch.object(wb, "request", return_value={"error": False, "data": {"id": 123}}) as send:
            self.assertEqual(self.run_command("price", "--shops", "2", "--name", "旅行杯", "--price", "129"), 0)
            send.assert_not_called()
            for action, options, field, value in (("price", ["--price", "129"], "price", 129),
                                                  ("discount", ["--target", "35"], "discount", 35)):
                send.reset_mock()
                self.assertEqual(self.run_command(action, "--shops", "2", "--name", "旅行杯", *options, "--apply"), 0)
                self.assertEqual(len(send.call_args_list), 2)
                self.assertEqual([c.args[2].split("=")[-1] for c in send.call_args_list], ["true", "false"])
                for sent in send.call_args.kwargs["json"]["data"]:
                    self.assertEqual(sent[field], value)
                    self.assertIn(sent["nmID"], {201, 202})
                    if action == "discount":
                        self.assertNotIn("price", sent)
                self.assertEqual(len(send.call_args.kwargs["json"]["data"]), 2)

    def test_contains_stock_uses_each_shops_warehouse_and_specs(self):
        from sellerops.integrations.wildberries import stocks
        with patch.object(stocks, "post_stocks", return_value={"error": False}) as post:
            self.assertEqual(self.run_command("stock", "--shops", "1,2", "--name-contains", "旅行杯",
                                              "--amount", "25", "--apply"), 0)
        self.assertEqual([(c.args[1], c.args[2]) for c in post.call_args_list], [
            (30 + sid, [{"chrtId": sid * 1000 + n, "amount": 25} for n in (1, 2, 3)]) for sid in (1, 2)])
        [receipt] = paths.daily_log_dir().glob("商品操作回执_*.csv")
        with receipt.open(encoding="utf-8-sig") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 6)
        self.assertFalse((Path(paths.current.log_dir) / "ops_result.csv").exists())

    def test_partial_response_keeps_chinese_receipts_and_stops_this_shops_batches(self):
        from sellerops.integrations.wildberries import stocks
        def submit(session, wh, items):
            if wh == 31:
                raise wb.common.CookieExpiredError("WB 403")
            if items[0]["chrtId"] == 2002:
                raise PlatformApiError("提交结果未知")
            return {"error": False}
        with patch.object(stocks, "post_stocks", side_effect=submit) as post:
            rc = self.run_command("stock", "--shops", "1,2", "--name", "旅行杯", "--amount", "25",
                                  "--apply", "--chunk", "1", "--interval", "0")
        self.assertEqual(rc, 1)
        self.assertEqual([call.args[1] for call in post.call_args_list], [31, 32, 32])
        [file] = paths.daily_log_dir().glob("商品操作回执_*.csv")
        with file.open(encoding="utf-8-sig") as stream:
            detail = list(csv.DictReader(stream))
        self.assertEqual([row["状态"] for row in detail], ["已阻止", "已阻止", "已受理", "结果未知"])
        self.assertEqual({row["商品中文名"] for row in detail}, {"旅行杯"})
        self.assertEqual([row["完整VC"] for row in detail], ["BCS-AAAA-1", "BCS-AAAA-2"] * 2)

    def test_stock_reads_only_the_selected_snapshot_once_and_needs_no_price_column(self):
        book = openpyxl.load_workbook(paths.current.price_book_xlsx)
        book.active.delete_cols(3)
        book.save(paths.current.price_book_xlsx)
        book.close()
        with patch.object(Snapshots, "load_shop_rows", wraps=Snapshots.load_shop_rows) as load:
            self.assertEqual(self.run_command("stock", "--shops", "2", "--name", "旅行杯", "--amount", "9"), 0)
        load.assert_called_once_with(2)

    def test_foreign_shop_id_in_legacy_snapshot_blocks_write(self):
        corrupt_snapshot(1, row_json='{"shopId": 2, "nmId": 200, "vendorCode": "BCS-AAAA-1"}')
        with patch.object(wb, "request") as send, redirect_stderr(StringIO()):
            self.assertEqual(self.run_command("price", "--shops", "1", "--vc", "BCS-AAAA-1",
                                              "--price", "129", "--apply"), 1)
        send.assert_not_called()

    def test_data_directories_isolate_warehouse_receipts_and_snapshot(self):
        from sellerops.persistence.product_results import ProductReceipt, write_product_receipts
        first = paths.current
        self.assertEqual(warehouse.resolve_warehouse(1), 31)
        receipt = write_product_receipts(first, [ProductReceipt(1, {"chrtId": 1}, "stock", 0, "accepted")])
        second = paths.configure(self.folder / "second")
        with patch.object(warehouse, "list_stores", return_value=[{"id": 81, "name": "莫斯科"}]):
            self.assertEqual(warehouse.resolve_warehouse(1), 81)
        self.assertIsNone(Snapshots.load_shop_rows(1))
        self.assertFalse(Path(second.log_dir).exists())
        self.assertEqual(receipt.parent, paths.daily_log_dir(data=first))
        self.assertTrue(receipt.is_file())

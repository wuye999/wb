"""WorkBuddy CLI contract with synthetic workbooks, snapshots and transport replies."""

from catalog_fixture import map_vcs
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import openpyxl
import requests

from operation_fixture import run_business_cli as main
from sellerops.core.errors import AuthenticationError, PlatformApiError
from sellerops.core.files import atomic_dump_json
from sellerops.domain.product_reports import ProductReport, ReportIssue
from sellerops.features.catalog import mapping_sync, products
from sellerops.features.products import price_review, product_data, warehouse
from sellerops.features.reports import product_report
from sellerops.features.system import daily
from sellerops.integrations.bcs.client import OpenAPIShop, client as bcs
from sellerops.integrations.wildberries import client as wb, stocks as wb_stock
from sellerops.persistence.snapshots import ProductSnapshotRepository as Snapshots
from sellerops.settings import paths

A, B, AMBIGUOUS = "BCS-AAAA-1", "BCS-BBBB-2", "BCS-OLDX-3"


def goods(nm, vc, discount):
    return {"nmID": nm, "vendorCode": vc, "discount": discount}


def page(*items):
    return {"error": False, "data": {"listGoods": list(items)}}


def accepted(task=700):
    return {"error": False, "data": {"id": task}}


class TestWorkBuddyProducts(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.callback(paths.configure, paths.current.data_dir)
        self.folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        paths.configure(self.folder)
        self.shop_config = {sid: {"shopId": sid, "shopName": f"店{sid}"} for sid in (1, 2)}
        cred = Mock(shops=list(self.shop_config.values()), root_version="fixture")
        cred.wb_shop.side_effect = self.shop_config.get
        stack.enter_context(patch.object(wb.credentials, "get", return_value=cred))
        stack.enter_context(patch.object(wb, "make_session",
                                        side_effect=lambda shop, *a: Mock(shop_id=shop["shopId"])))
        stack.enter_context(patch.object(warehouse, "list_stores", side_effect=lambda sid: [
            {"id": 10 + sid, "name": "莫斯科", "warehouseId": 999}, {"id": 99, "name": "成都"}]))
        stack.enter_context(patch.object(wb.time, "sleep"))
        fetch = stack.enter_context(patch.object(products, "fetch_all"))
        merge = stack.enter_context(patch.object(mapping_sync, "merge"))
        sync = stack.enter_context(patch.object(bcs, "sync_shops_parallel"))
        self.addCleanup(fetch.assert_not_called)
        self.addCleanup(merge.assert_not_called)
        self.addCleanup(sync.assert_not_called)
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Sheet1"
        sheet.append(["卖家SKU", "产品中文名", "实际售价", "尺寸", "vendorCode前缀码"])
        sheet.append(["A", "旅行杯", 60, "1*2*3/0.5", "AAAA"])
        sheet.append(["B", "旅行杯", 90, "4*5*6/0.7", "BBBB"])
        book.save(paths.current.price_book_xlsx)
        book.close()
        self.save(1, self.row(A, 101, (201,)), self.row(B, 102, (202,)))
        self.save(2, self.row(A, 301, (201,)))
        map_vcs({A: 'A', B: 'B'}, source='prefix')

    @staticmethod
    def row(vc, nm, specs, *, price=99.9, discount=50):
        return {"vendorCode": vc, "nmId": nm, "discount": discount,
                "sizeList": [{"chrtId": chrt, "price": price} for chrt in specs]}

    @staticmethod
    def save(sid, *rows):
        Snapshots.save_shop_products(sid, list(rows))

    def command(self, *argv):
        output, err = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(err):
            rc = main(["--data-dir", str(self.folder), *argv])
        lines = output.getvalue().splitlines()
        self.assertTrue(lines, err.getvalue())
        summary = json.loads(lines[-1])
        self.assertEqual(summary["schema_version"], 1)
        for value in summary["artifacts"].values():
            self.assertTrue(Path(value).is_file(), value)
        return rc, summary

    def test_sku_and_exact_name_keep_their_price_rows_through_all_callers(self):
        with patch.object(wb, "request", return_value=accepted()) as send:
            rc, result = self.command("product", "price", "--shops", "1", "--sku", "B", "--apply")
            self.assertEqual((rc, result["fields"]), (0, ["price"]))
            [row] = send.call_args.kwargs["json"]["data"]
            self.assertEqual(row, {"nmID": 102, "vendorCode": B, "currencyIsoCode": "CNY", "price": 90})
            self.assertEqual(send.call_count, 2)
            self.assertEqual(result["per_shop"][0]["task_ids"], ["700"])
            self.assertEqual(result["per_shop"][0]["planned_price"], {"min": 90, "max": 90})
            rc, result = self.command("product", "price", "--shops", "1", "--name", "旅行杯", "--apply")
            self.assertEqual(rc, 0)
            self.assertEqual(result["per_shop"][0]["planned_price"], {"min": 60, "max": 90})
            self.assertEqual({row["nmID"]: row["price"] for row in send.call_args.kwargs["json"]["data"]},
                             {101: 60, 102: 90})
        with patch.object(wb_stock, "post_stocks", return_value={"error": False}) as stock:
            rc, result = self.command("product", "stock", "--shops", "1", "--name", "旅行杯",
                                      "--amount", "25", "--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(stock.call_args.args[1:], (11, [{"chrtId": 201, "amount": 25}, {"chrtId": 202, "amount": 25}]))
        self.assertEqual(result["per_shop"][0]["specs"]["accepted"], 2)
        with patch.object(bcs, "post", return_value={"code": 200}) as dimensions, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir", str(self.folder), "product", "dimensions", "--shops", "1",
                                   "--name", "旅行杯", "--apply"]), 0)
        self.assertEqual([(r["nmId"], r["length"]) for r in dimensions.call_args.args[1]["dataList"]],
                         [(101, 1.0), (102, 4.0)])

    def test_only_confirmed_sku_rows_submit(self):
        self.save(1, self.row(A, 101, (201,)), self.row(B, 102, (202,)),
                  self.row(AMBIGUOUS, 103, (203,)))
        with patch.object(wb, "request", return_value=accepted()) as send:
            rc, result = self.command("product", "price", "--shops", "1", "--sku", "B", "--apply")
        self.assertEqual((rc, result["status"]), (0, "completed"))
        self.assertEqual([r["nmID"] for r in send.call_args.kwargs["json"]["data"]], [102])

    def test_field_intents_validate_before_data_and_bcs_keeps_exact_supported_price(self):
        cases = [("price", "--keep-price"), ("price", "--discount", "0"),
                 ("price", "--channel", "bcs", "--club-discount", "10"),
                 ("price", "--price", "80", "--from-price-book"), ("discount",),
                 ("discount", "--target", "-1"), ("discount", "--target", "50", "--chunk", "301")]
        for action, *options in cases:
            for apply in ([], ["--apply"]):
                with self.subTest(action=action, options=options, apply=apply), redirect_stdout(io.StringIO()), \
                        redirect_stderr(io.StringIO()), patch.object(Snapshots, "load_shop_rows") as read, \
                        patch.object(wb, "request") as wb_write, patch.object(bcs, "post") as bcs_write:
                    args = ["--data-dir", str(self.folder), "product", action, "--shops", "1", "--vc", A, *options, *apply]
                    try:
                        rc = main(args)
                    except SystemExit as error:
                        rc = error.code
                    self.assertNotEqual(rc, 0)
                read.assert_not_called()
                wb_write.assert_not_called()
                bcs_write.assert_not_called()
        self.save(1, {**self.row(A, 101, (201, 202)), "sizeList": [
            {"chrtId": 201, "price": 99.9}, {"chrtId": 202, "price": 129}]})
        with patch.object(wb, "request", return_value=accepted()) as send, patch.object(bcs, "post") as submit:
            rc, _ = self.command("product", "price", "--shops", "1", "--vc", A, "--keep-price",
                                 "--discount", "0", "--apply")
            self.assertEqual(rc, 0)
            self.assertEqual(send.call_args.kwargs["json"]["data"],
                             [{"nmID": 101, "vendorCode": A, "currencyIsoCode": "CNY", "discount": 0}])
            rc, summary = self.command("product", "price", "--shops", "1", "--vc", A, "--channel", "bcs",
                                       "--keep-price", "--discount", "0", "--apply")
            self.assertEqual((rc, summary["per_shop"][0]["products"]["blocked"]), (1, 1))
            submit.assert_not_called()
        for amount, expect in ((99.95, 0), (99.951, 1)):
            self.save(1, self.row(A, 101, (201, 202), price=amount))
            with patch.object(bcs, "batch_set_prices", return_value={"code": 200, "taskId": "BCS1"}) as submit:
                rc, summary = self.command("product", "price", "--shops", "1", "--vc", A, "--channel", "bcs",
                                           "--keep-price", "--club-discount", "10", "--apply")
                self.assertEqual(rc, expect)
                if expect:
                    submit.assert_not_called()
                else:
                    submit.assert_called_once_with(1, [{"nmID": 101, "price": 99.95, "discount": None, "clubDiscount": 10}])
                    self.assertEqual(summary["fields"], ["clubDiscount"])
                    self.assertEqual(summary["per_shop"][0]["task_ids"], ["BCS1"])

    def test_targeted_discount_assignment_and_current_threshold(self):
        self.save(1, self.row(A, 101, (201,), discount=50), self.row(B, 102, (202,), discount=60))
        with patch.object(wb, "request", return_value=accepted()) as send, patch.object(bcs, "fetch_open_api_shops") as keys:
            for value in ("30", "50"):
                rc, result = self.command("product", "discount", "--shops", "1", "--vc", A,
                                          "--target", value, "--apply")
                self.assertEqual((rc, result["per_shop"][0]["products"]["accepted"]), (0, 1))
            self.assertEqual([c.kwargs["json"]["data"][0]["discount"] for c in send.call_args_list], [30, 30, 50, 50])
            keys.assert_not_called()
        events = []
        api = Mock()
        api.prices.side_effect = lambda ids: (events.append(("read", ids)) or [
            goods(101, A, 60), goods(102, B, 40)])
        def submit(session, method, url, **kwargs):
            events.append(("write", [row["nmID"] for row in kwargs["json"]["data"]]))
            return accepted()
        with patch.object(wb, "request", side_effect=submit), \
                patch.object(bcs, "fetch_open_api_shops", return_value=[OpenAPIShop(1, "店1", "fixture")]), \
                patch.object(product_data, "WBOpenAPI", return_value=api):
            args = ("product", "discount", "--shops", "1", "--name", "旅行杯",
                    "--threshold", "50", "--target", "20")
            rc, preview = self.command(*args)
            self.assertEqual((rc, preview["status"]), (0, "preview"))
            self.assertEqual(preview["per_shop"][0]["products"]["accepted"], 0)
            self.assertEqual(self.command(*args, "--apply")[0], 0)
            self.assertEqual(events, [("read", [101, 102]), ("read", [101, 102]), ("write", [101]), ("write", [101])])
            api.prices.side_effect = None
            api.prices.return_value = [goods(101, "wrong", 60)]
            events.clear()
            rc, summary = self.command(*args, "--apply")
            self.assertEqual((rc, summary["per_shop"][0]["products"]["blocked"]), (1, 2))
            self.assertEqual(events, [])

    def test_price_floor_and_cross_card_stock_collisions_use_one_plan(self):
        for apply in ((), ("--apply",)):
            with patch.object(wb, "request") as send:
                rc, summary = self.command("product", "price", "--shops", "1", "--vc", A, "--price", "40", *apply)
            self.assertEqual(rc, 1)
            self.assertEqual((summary["per_shop"][0]["products"]["submittable"],
                              summary["per_shop"][0]["products"]["blocked"]), (0, 1))
            send.assert_not_called()
        self.save(1, self.row(A, 101, (201,)), self.row(B, 102, (201,)))
        with patch.object(wb_stock, "post_stocks", return_value={"error": False}) as submit:
            rc, summary = self.command("product", "stock", "--shops", "1,2", "--vc", A, "--amount", "25", "--apply")
        self.assertEqual((rc, summary["status"]), (1, "partial"))
        self.assertEqual(submit.call_args.args[1:], (12, [{"chrtId": 201, "amount": 25}]))
        submit.assert_called_once()
        with patch.object(wb_stock, "post_stocks") as zero, patch.object(wb, "request_post") as trash, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir", str(self.folder), "product", "trash", "--shops", "1",
                                   "--vc", A, "--apply", "--yes"]), 1)
        zero.assert_not_called()
        trash.assert_not_called()

    def test_wb_read_ranges_empty_errors_limit_and_existing_daily_scope(self):
        def submit(session, method, url, **kwargs):
            if url == wb.WBClient.DISC_LIST:
                return page(goods(101, A, 70)) if session.shop_id == 1 else {"error": True}
            return accepted(802)
        with patch.object(wb, "request", side_effect=submit) as send, patch.object(bcs, "post") as bcs_write:
            args = daily.build_steps("adjust-discounts", True, "1,2")[0][5:]
            self.assertEqual(args[:2], ['ops', 'discount'])
            self.assertNotIn('--apply', args)
            self.assertEqual(args[args.index("--target") + 1], "50")
            rc, result = self.command('product', *args[1:])
        self.assertEqual((rc, result["status"], result["channel"]), (1, "failed", "wb"))
        self.assertIsNone(result["per_shop"][1]["products"]["matched"])
        self.assertEqual([c.args[0].shop_id for c in send.call_args_list if "upload/task" in c.args[2]], [])
        bcs_write.assert_not_called()
        args = ("product", "discount", "--shops", "1", "--all-products", "--target", "50")
        for packet, expected in ((page(), "no_match"), ({"error": False, "data": {}}, "failed")):
            with patch.object(wb, "request", return_value=packet) as send:
                rc, result = self.command(*args, "--apply")
            self.assertEqual(result["status"], expected)
            self.assertEqual(rc, int(expected == "failed"))
            send.assert_called_once()
        real = wb.WBClient.fetch_discount_goods_desc
        def one_page(self, *a, **kw):
            return real(self, *a, **kw, page_size=2, max_pages=1)
        with patch.object(wb.WBClient, "fetch_discount_goods_desc", one_page), \
                patch.object(wb, "request", side_effect=[page(goods(101, A, 50), goods(102, B, 45)),
                                                         accepted(), accepted()]) as send:
            self.assertEqual(self.command(*args, "--limit", "1", "--apply")[0], 0)
            self.assertEqual(send.call_args.kwargs["json"]["data"][0]["nmID"], 102)
        with patch.object(wb.WBClient, "fetch_discount_goods_desc", one_page), \
                patch.object(wb, "request", return_value=page(goods(101, A, 50), goods(102, B, 45))) as send:
            self.assertEqual(self.command(*args, "--apply")[0], 1)
            send.assert_called_once()

    def test_wb_repeated_page_and_dual_thresholds_are_safe(self):
        with patch.object(wb, "request", return_value=page(goods(101, A, 70))) as read:
            client = wb.WBClient(self.shop_config[1])
            with self.assertRaisesRegex(PlatformApiError, "重复商品"):
                client.fetch_discount_goods_desc(60, page_size=1)
            self.assertEqual(read.call_count, 2)
        mixed = [page(goods(101, A, 70), goods(102, B, 55), goods(103, AMBIGUOUS, 30)),
                 page(goods(103, AMBIGUOUS, 30), goods(102, B, 55), goods(101, A, 70)),
                 accepted(), accepted()]
        with patch.object(wb, "request", side_effect=mixed) as send:
            rc, result = self.command("product", "discount", "--shops", "1", "--all-products", "--target", "50",
                                      "--threshold", "52", "--below", "60", "--apply")
        self.assertEqual((rc, result["per_shop"][0]["products"]["accepted"]), (0, 3))
        self.assertEqual([r["nmID"] for r in send.call_args.kwargs["json"]["data"]], [101, 102, 103])

    def test_wb_auth_and_unknown_do_not_replay_and_review_has_separate_receipts(self):
        with patch.object(wb, "request", side_effect=[accepted(), accepted(), requests.Timeout(),
                                                     AuthenticationError("WB 403")]) as send, \
                patch.object(bcs, "post") as bcs_write:
            self.save(2, self.row(A, 301, (201,)), self.row(B, 302, (202,)))
            rc, summary = self.command("product", "price", "--shops", "1,2", "--name", "旅行杯",
                                       "--price", "90", "--chunk", "1", "--apply")
        self.assertEqual((rc, summary["status"]), (1, "partial"))
        self.assertEqual(send.call_count, 4)
        bcs_write.assert_not_called()
        first, second = summary["per_shop"]
        self.assertEqual((first["products"]["accepted"], first["products"]["unknown"],
                          second["products"]["blocked"]), (1, 1, 2))
        with patch.object(wb, "request", return_value=accepted()), \
                patch.object(price_review, "fetch_all_quarantine", return_value=[{"nmID": 101, "id": 9, "newPrice": 60}]), \
                patch.object(price_review, "apply_prices", return_value={"error": True, "errorText": "rejected"}) as apply:
            rc, summary = self.command("product", "price", "--shops", "1", "--sku", "A", "--auto-review", "--apply")
        self.assertEqual((rc, summary["status"]), (1, "partial"))
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[1], [9])
        self.assertEqual(summary["per_shop"][0]["products"]["accepted"], 1)
        self.assertEqual(summary["per_shop"][0]["price_review"]["failed"], 1)

    def test_bcs_stock_and_discount_emit_same_receipt_states_and_task_ids(self):
        with patch.object(bcs, "batch_set_stock", return_value={"code": 200, "data": {"taskId": "ST1"}}) as stock:
            rc, result = self.command("product", "stock", "--channel", "bcs", "--shops", "1", "--sku", "B",
                                      "--amount", "3", "--apply")
        self.assertEqual((rc, result["channel"], result["per_shop"][0]["task_ids"]), (0, "bcs", ["ST1"]))
        stock.assert_called_once_with(1, 11, [{"chrtId": 202, "amount": 3}])

        raw = [self.row(A, 101, (201, 203), price=70.25, discount=65),
               self.row(B, 102, (202,), price=90, discount=40)]
        with patch.object(bcs, "fetch_shop_list", return_value=[{"id": 1, "name": "店1"}]), \
                patch.object(bcs, "fetch_shop_products", return_value=raw) as read, \
                patch.object(bcs, "batch_set_prices", return_value={"code": 200, "taskId": "DS1"}) as write:
            rc, result = self.command("product", "discount", "--channel", "bcs", "--shops", "1", "--all-products",
                                      "--threshold", "50", "--target", "25", "--apply")
        self.assertEqual((rc, result["fields"], result["per_shop"][0]["task_ids"]), (0, ["discount"], ["DS1"]))
        read.assert_called_once_with(1, "BASE")
        write.assert_called_once_with(1, [{"nmID": 101, "price": 70.25, "discount": 25, "clubDiscount": None}])
        with Path(result["artifacts"]["discount_csv"]).open(encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({r["结果"] for r in rows}, {"正常跳过 折扣不满足阈值或已等于目标", "已受理 taskId=DS1"})

    def test_official_discount_auth_failure_keeps_other_store_independent(self):
        denied, healthy = Mock(), Mock()
        denied.prices.side_effect = AuthenticationError("WB 403")
        healthy.prices.return_value = [goods(301, A, 80)]
        with patch.object(bcs, "fetch_open_api_shops", return_value=[
            OpenAPIShop(1, "店1", "one"), OpenAPIShop(2, "店2", "two")]), \
                patch.object(product_data, "WBOpenAPI", side_effect=[denied, healthy]), \
                patch.object(wb, "request", return_value=accepted()) as send:
            rc, result = self.command("product", "discount", "--shops", "1,2", "--name", "旅行杯",
                                      "--threshold", "50", "--target", "30", "--apply")
        self.assertEqual((rc, result["status"]), (1, "partial"))
        denied.prices.assert_called_once_with([101, 102])
        healthy.prices.assert_called_once_with([301])
        self.assertEqual(result["per_shop"][0]["products"]["blocked"], 2)
        self.assertEqual([c.args[0].shop_id for c in send.call_args_list], [2, 2])

    def test_from_price_book_requires_valid_row_and_report_cannot_supply_old_price(self):
        book = openpyxl.load_workbook(paths.current.price_book_xlsx)
        book.active["C3"] = None
        book.save(paths.current.price_book_xlsx)
        book.close()
        history = openpyxl.Workbook()
        history.active.title = "映射总表"
        history.active.append(["产品中文名", "完整VC", "实际售价"])
        from sellerops.persistence.headers import COL_CN, COL_VC
        history.active["A1"] = COL_CN
        history.active["B1"] = COL_VC
        history.active.append(["旅行杯", B, 85])
        history.save(paths.current.mapping_xlsx)
        history.close()
        args = ("product", "price", "--shops", "1", "--sku", "B", "--apply")
        with patch.object(wb, "request", return_value=accepted()) as send:
            rc, result = self.command(*args, "--from-price-book", "--discount", "10")
            self.assertEqual((rc, result["per_shop"][0]["products"]["blocked"]), (1, 1))
            send.assert_not_called()
            self.assertEqual(self.command(*args)[0], 1)
            send.assert_not_called()
        with patch.object(wb, "request", return_value=accepted()) as send:
            self.assertEqual(self.command(*args, "--price", "90", "--discount", "10")[0], 0)
            self.assertEqual(send.call_args.kwargs["json"]["data"][0],
                             {"nmID": 102, "vendorCode": B, "currencyIsoCode": "CNY", "price": 90, "discount": 10})

    def test_publishing_keeps_initial_stock_in_bcs_push(self):
        from sellerops.domain.models import PublishItem
        from sellerops.features.publishing import bulk_publish
        item = PublishItem(nm_id=123, price=90, prefix="BCS-BBBB", vendor_code="BCS-BBBB-123",
                           length=4, width=5, height=6, weight=0.7, stock=17, target_shops=[1])
        with patch.object(bcs, "fetch_shop_list", return_value=[{"id": 1, "name": "店1"}]), \
                patch.object(bulk_publish, "resolve_publish_details", return_value=(item, None)), \
                patch.object(bcs, "post", return_value={"code": 200, "data": {"taskId": "push"}}) as post, \
                patch.object(wb_stock, "post_stocks") as stock, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir", str(self.folder), "listing", "publish", "123",
                                   "--shops", "1", "--stock", "17", "--apply"]), 0)
        post.assert_called_once()
        self.assertTrue(post.call_args.args[0].endswith("/products/batch/push"))
        self.assertEqual(post.call_args.args[1]["shop"], [{"id": 1, "warehouseId": 11, "warehouseQuantity": 17}])
        stock.assert_not_called()

    def test_partial_report_links_workbook_and_uses_existing_coverage(self):
        report = ProductReport("2026-11-22T11:00:00+08:00", "2026-11-01", "2026-11-22",
                               shops=[{"id": 1, "name": "店1", "account_source": "本机账号", "role": "本机目标店铺"}],
                               target_shop_ids={1}, coverage={1: {"orders": "未知", "feedbacks": "完整", "snapshot": "可读取"}},
                               issues=[ReportIssue("订单", "离线模拟失败", 1)])
        output = self.folder / "报告.xlsx"
        with patch.object(product_report, "collect", return_value=report):
            rc, result = self.command("report", "products", "--out", str(output))
        self.assertEqual((rc, result["status"], result["shops"]), (1, "partial", [1]))
        self.assertFalse(result["coverage_complete"])
        self.assertIsNone(result["return_metrics"]["return_rate"])
        self.assertEqual(result["artifacts"]["workbook"], str(output.resolve()))
        workbook = openpyxl.load_workbook(output, read_only=True)
        self.addCleanup(workbook.close)
        self.assertIn("异常与缺失", workbook.sheetnames)
        self.assertIn("离线模拟失败", [row[-1] for row in workbook["异常与缺失"].values])

"""Offline coverage for the rating report's platform reads and CLI outputs."""
from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from catalog_fixture import map_vcs, snapshot, write_book
from sellerops.cli.main import main
from sellerops.core.errors import AuthenticationError, RateLimitError
from sellerops.features.reports import ratings
from sellerops.integrations.bcs import client as bcs
from sellerops.integrations.wildberries.open_api import OpenAPIResponseError, WBOpenAPI
from sellerops.settings import paths


NOW = datetime(2026, 11, 25, 10, tzinfo=ratings.TZ)
TODAY = "2026-11-25T03:00:00Z"
FIRST = "2026-11-18T16:00:00Z"
LAST = "2026-11-25T15:59:59.999Z"
TOMORROW = "2026-11-25T16:00:00Z"
START_EPOCH = int(datetime(2026, 11, 19, tzinfo=ratings.TZ).timestamp())
END_EPOCH = int(datetime(2026, 11, 26, tzinfo=ratings.TZ).timestamp())
VC = "BCS-AAAA-123"


def feedback(fid, stars, *, nm=101, created=TODAY, vc=VC, title="Test product"):
    return {"id": fid, "createdDate": created, "productValuation": stars,
            "productDetails": {"nmId": nm, "supplierArticle": vc, "productName": title}}


class TestRatings(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "data"
        self.addCleanup(paths.configure, paths.current.data_dir)
        paths.configure(self.data)
        write_book([("S1", "旅行杯", 55, "AAAA", "", "")])
        map_vcs({VC: "S1"})
        window = ratings._feedback_window
        window_patch = patch.object(ratings, "_feedback_window", side_effect=lambda days: window(days, NOW))
        window_patch.start()
        self.addCleanup(window_patch.stop)
        bcs_patch = patch.object(ratings, "BCSClient")
        self.bcs = bcs_patch.start()
        self.addCleanup(bcs_patch.stop)
        self.directory = self.bcs.return_value.fetch_open_api_shops
        self.directory.return_value = [bcs.OpenAPIShop(11, "店甲", "fixture-private-key")]

    def cli(self, *args, workbook=False):
        output, stderr = io.StringIO(), io.StringIO()
        target = self.data / ("rating.xlsx" if workbook else "rating.csv")
        with redirect_stdout(output), redirect_stderr(stderr):
            code = main(["--data-dir", str(self.data), "report", "ratings", *args, "--out", str(target), "--json"])
        reply = json.loads(output.getvalue())
        self.assertNotIn("fixture-private-key", output.getvalue() + stderr.getvalue())
        return code, reply, stderr.getvalue()

    def csv_rows(self, reply):
        path = Path(reply["data"]["artifacts"]["csv"])
        self.assertNotIn("fixture-private-key", path.read_text(encoding="utf-8-sig"))
        with path.open(encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))

    def test_full_cli_reads_three_paths_deduplicates_and_applies_utc8_bounds(self):
        self.directory.return_value += [bcs.OpenAPIShop(12, "店乙", "fixture-private-key-b")]
        first = feedback("first", 1, created=FIRST)
        last = feedback("last", 5, created=LAST, title="Latest title")
        earlier = feedback("old", True, nm=None, created="2026-11-18T15:59:59Z")
        future = feedback("future", 3, created=TOMORROW)

        def request(api, service, method, path, *, params):
            self.assertEqual((api.region, service, method), ("cn", "feedbacks-api", "GET"))
            archive = path.endswith("/archive")
            if archive:
                self.assertNotIn("dateFrom", params)
                self.assertNotIn("dateTo", params)
            else:
                self.assertEqual(params["dateFrom"], START_EPOCH - 1)
                self.assertEqual(params["dateTo"], END_EPOCH)
            rows = []
            if params["skip"] == 0:
                if api.session.headers["Authorization"].endswith("-b"):
                    if archive:
                        rows = [feedback("first", 2)]
                else:
                    rows = ([first, last, earlier, future] if archive else
                            [first] if params["isAnswered"] == "false" else [first, last])
            return {"data": {"feedbacks": rows}}

        with patch.object(WBOpenAPI, "_request", autospec=True, side_effect=request) as read:
            rc, reply, _ = self.cli("--days", "7", "--wb-region", "cn", workbook=True)
        self.assertEqual(rc, 0)
        self.bcs.assert_called_once_with(auto_refresh=False)
        self.directory.assert_called_once()
        self.assertEqual(read.call_count, 10)
        self.assertEqual(reply["data"]["period"], {"from": "2026-11-19", "through": "2026-11-25", "timezone": "UTC+8"})
        self.assertEqual(reply["data"]["wb_region"], "cn")
        workbook = load_workbook(reply["data"]["artifacts"]["workbook"], read_only=True)
        try:
            rows = list(workbook.active.values)
        finally:
            workbook.close()
        self.assertEqual(list(rows[0]), ratings.REPORT_HEADERS)
        self.assertEqual(rows[1][7:16], ("店甲", 3, 2, 1, 0, 0, 0, 1, "Latest title"))
        self.assertEqual(rows[2][7:15], ("店乙", 2, 1, 0, 1, 0, 0, 0))
        self.assertNotIn("fixture-private-key", str(rows))

    def test_days_zero_requests_all_history_and_still_applies_upper_bound(self):
        api = Mock()
        api.feedbacks.return_value = [feedback("old", 4, created="2019-03-01T00:00:00Z"),
                                      feedback("last", 5, created=LAST),
                                      feedback("future", 1, created=TOMORROW)]
        with patch.object(ratings, "WBOpenAPI", return_value=api) as factory:
            rc, reply, stderr = self.cli("--days", "0")
        self.assertEqual(rc, 0)
        self.assertIsNone(reply["data"]["period"]["from"])
        self.assertIn("接口可访问的全部历史", stderr)
        factory.assert_called_once_with("fixture-private-key", region="ru")
        api.feedbacks.assert_called_once_with(date_from=None, date_to=END_EPOCH)
        api.close.assert_called_once()
        [row] = self.csv_rows(reply)
        self.assertEqual((row["评分"], row["评价总数"], row["1星数"]), ("4.5", "2", "0"))

    def test_filters_match_products_and_limit_is_applied_after_the_full_aggregate(self):
        api = Mock()
        api.feedbacks.return_value = [feedback("good", 5), feedback("bad", 1),
                                      *[feedback(str(i), 2, nm=102, vc="BCS-BBBB-456") for i in range(3)]]
        with patch.object(ratings, "WBOpenAPI", return_value=api):
            rc, reply, _ = self.cli("--stars", "4,5,5", "--name-contains", "杯", "--prefix", "aaaa",
                                    "--vc", "aaaa-123", "--rating", "3.05", "--min-rating", "3", "--max-rating", "3")
            self.assertEqual(rc, 0)
            [row] = self.csv_rows(reply)
            self.assertEqual((row["产品中文名"], row["最低售价"], row["1星数"], row["5星数"]), ("旅行杯", "55.0", "1", "1"))
            rc, reply, _ = self.cli("--bad", "--one-star", "--min-one-star", "1", "--name", "旅行杯")
            self.assertEqual((rc, reply["data"]["rows"]), (0, 1))
            rc, reply, _ = self.cli("--stars", "4,5", "--limit", "1")
            self.assertEqual((rc, reply["data"]["rows"]), (0, 0))
            rc, reply, _ = self.cli("--two-star")
            self.assertEqual((rc, reply["data"]["rows"]), (0, 1))
        self.assertEqual(api.feedbacks.call_count, 4)

    def test_zero_feedback_adds_only_in_scope_snapshot_cards_and_fills_missing_vc(self):
        snapshot(11, [{"nmId": 101, "vendorCode": VC, "title": "Local product"},
                      {"nmId": 102, "vendorCode": "BCS-ZERO-1"},
                      {"nmId": 103, "vendorCode": "BCS-TRSH-1", "trashedAt": "2026-11-01"}],
                 fetchedAt="2026-11-23")
        snapshot(12, [{"nmId": 109, "vendorCode": "BCS-ELSE-1"}])
        api = Mock()
        api.feedbacks.return_value = [feedback("mapped", 5, vc="", title=""),
                                      feedback("conflict-1", 1, nm=104, vc="BCS-CNFL-1"),
                                      feedback("conflict-2", 2, nm=104, vc="BCS-CNFL-2")]
        with patch.object(ratings, "WBOpenAPI", return_value=api):
            rc, reply, stderr = self.cli("--min-feedbacks", "0")
            self.assertEqual(rc, 0)
            rows = {row["wb编号"]: row for row in self.csv_rows(reply)}
            self.assertEqual(set(rows), {"101", "102", "104"})
            self.assertEqual((rows["101"]["BCS编号"], rows["101"]["商品名称"]), (VC, "Local product"))
            self.assertEqual((rows["102"]["评分"], rows["102"]["评价总数"], rows["102"]["wb编号来源"]),
                             ("0.0", "0", "本店快照"))
            self.assertEqual((rows["104"]["BCS编号"], rows["104"]["评价总数"]), ("", "2"))
            self.assertIn("冲突", stderr)
            self.assertIn("2026-11-23", stderr)
            rc, reply, _ = self.cli("--min-feedbacks", "0", "--max-rating", "2")
            self.assertEqual((rc, reply["data"]["rows"]), (0, 1))
            [row] = self.csv_rows(reply)
            self.assertEqual(row["wb编号"], "104")

    def test_missing_snapshot_retains_confirmed_ratings_but_is_partial_for_zero_rows(self):
        api = Mock()
        api.feedbacks.return_value = [feedback("available", 4)]
        with patch.object(ratings, "WBOpenAPI", return_value=api):
            rc, reply, _ = self.cli("--min-feedbacks", "0")
        self.assertEqual((rc, reply["data"]["rows"]), (1, 1))
        self.assertIn("零评价商品覆盖不完整", reply["data"]["failed_shops"][0]["error"])
        self.assertEqual(self.csv_rows(reply)[0]["评价总数"], "1")

    def test_store_failures_preserve_successful_rows_without_expanding_the_scope(self):
        snapshot(17, [])
        from sellerops.persistence import db
        with db.connect(write=True) as connection:
            connection.execute("UPDATE shops SET status='archived' WHERE shop_id='17'")
        self.directory.return_value += [
            bcs.OpenAPIShop(12, "缺密钥", ""), bcs.OpenAPIShop(13, "鉴权", "auth"),
            bcs.OpenAPIShop(14, "限流", "rate"), bcs.OpenAPIShop(15, "坏页", "bad"),
            bcs.OpenAPIShop(16, "窗内坏行", "row"), bcs.OpenAPIShop(17, "归档", "archived"),
            bcs.OpenAPIShop(19, "未选择", "outside"),
        ]
        apis = {
            "fixture-private-key": Mock(feedbacks=Mock(return_value=[feedback("available", 5)])),
            "auth": Mock(feedbacks=Mock(side_effect=AuthenticationError("fixture-private-key"))),
            "rate": Mock(feedbacks=Mock(side_effect=RateLimitError())),
            "bad": Mock(feedbacks=Mock(side_effect=OpenAPIResponseError("WB 评价页缺少 data.feedbacks"))),
            "row": Mock(feedbacks=Mock(return_value=[feedback("invalid", 7)])),
        }
        with patch.object(ratings, "WBOpenAPI", side_effect=lambda key, region: apis[key]) as factory:
            rc, reply, _ = self.cli("--shops", "11,12,13,14,15,16,17,18")
        self.assertEqual((rc, reply["data"]["rows"]), (1, 1))
        failed = {failure["shop_id"]: failure["error"] for failure in reply["data"]["failed_shops"]}
        self.assertEqual(set(failed), set(range(12, 19)))
        self.assertIn("权限", failed[13])
        self.assertIn("限流", failed[14])
        self.assertIn("星级", failed[16])
        self.assertIn("归档", failed[17])
        self.assertEqual(factory.call_count, len(apis))
        for api in apis.values():
            api.feedbacks.assert_called_once()
            api.close.assert_called_once()
        self.assertEqual(self.csv_rows(reply)[0]["来源的店铺"], "店甲")

    def test_empty_feedback_is_success_and_unparseable_date_is_a_failure(self):
        api = Mock()
        with patch.object(ratings, "WBOpenAPI", return_value=api):
            api.feedbacks.return_value = []
            rc, reply, _ = self.cli()
            self.assertEqual((rc, reply["summary"], reply["data"]["failed_shops"]), (0, "没有符合条件的评分记录", []))
            self.assertNotIn("artifacts", reply["data"])
            api.feedbacks.return_value = [feedback("bad-time", 5, created="invalid")]
            rc, reply, _ = self.cli("--days", "0")
            self.assertEqual((rc, reply["data"]["rows"]), (1, 0))
            self.assertIn("创建时间", reply["data"]["failed_shops"][0]["error"])
            self.assertNotIn("artifacts", reply["data"])

    def test_bcs_auth_failure_and_invalid_inputs_do_not_start_wb_calls(self):
        with patch.object(ratings, "WBOpenAPI") as factory:
            for args in (("--days", "-1"), ("--stars", "4,6"), ("--limit", "-1"), ("--shops", "abc"),
                         ("--rating", "nan")):
                rc, _, _ = self.cli(*args)
                self.assertEqual(rc, 1)
            self.directory.assert_not_called()
            self.directory.side_effect = AuthenticationError("fixture-private-key")
            rc, reply, _ = self.cli()
            self.assertEqual(rc, 1)
            self.assertIn("login bcs", reply["next_action"])
            factory.assert_not_called()


class TestSavedBCSAccess(unittest.TestCase):
    def test_bcs_read_does_not_renew_on_transport_or_business_authentication_failure(self):
        for status, code in ((401, None), (403, None), (200, 401), (200, 403)):
            saved = Mock(token="session", limit_key="limit", base_url=bcs.BCS_BASE_URL)
            response = Mock(status_code=status, json=Mock(return_value={"code": code}))
            with self.subTest(status=status, code=code), patch.object(bcs.credentials, "get", return_value=saved), \
                    patch.object(bcs, "auto_refresh_enabled") as enabled, patch.object(bcs, "refresh_login") as renew, \
                    patch.object(bcs.requests, "request", return_value=response) as request:
                with self.assertRaises(AuthenticationError):
                    bcs.BCSClient(auto_refresh=False).fetch_open_api_shops()
            enabled.assert_not_called()
            renew.assert_not_called()
            request.assert_called_once()
            self.assertIs(request.call_args.kwargs["allow_redirects"], False)

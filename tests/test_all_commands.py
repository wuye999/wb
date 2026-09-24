# -*- coding: utf-8 -*-
"""
wb_ops 全量命令与核心业务综合自动化测试套件

⚠ 日常不必全量跑（本套件真连平台，约 5 分钟）。按需测试请用选择器：
    python tests/run_tests.py --changed      # 只跑本次改动相关（推荐）
    python tests/run_tests.py --cmd appeals  # 只跑指定命令（新增/改命令时必跑）
    python tests/run_tests.py --help-smoke   # 最快回归：改动模块导入检查 + 全命令 --help
全量（跨层改动 / 发版）：
    python -m unittest tests/test_all_commands.py
"""
import os
import subprocess
import sys
import time
import unittest

PYTHON = sys.executable
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB_SCRIPT = os.path.join(BASE_DIR, "wb.py")


class TestAllCommands(unittest.TestCase):

    def _run_cmd(self, args, expect_code=0):
        cmd = [PYTHON, WB_SCRIPT] + args
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        res = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if expect_code is not None:
            self.assertEqual(
                res.returncode,
                expect_code,
                f"Command failed: {' '.join(args)}\nStderr: {res.stderr}\nStdout: {res.stdout}",
            )
        return res


    # ---------------- 1. 全部 49 个命令的帮助与解析测试 ----------------
    def test_01_all_subcommand_helps(self):
        subcommands = [
            "shops", "fetch", "mapping", "mapping-import", "mapping-check",
            "mismatch-check", "review", "merge", "mapping-rename", "shops-mapping",
            "price", "price-wb", "price-bcs", "stock", "stock-wb", "stock-bcs", "trash", "replicate", "import-shelve",
            "promo-apply", "promo-goods", "promo-gap", "discount", "discount-wb", "discount-scan", "discount-bcs",
            "dimension", "dims-check", "banned", "clean", "price-review",
            "orders", "questions", "questions-watch", "appeals", "ai-test", "mabang-orders",
            "mabang-forecast", "feishu-register", "mabang-stock-register",
            "mabang-stock-daily", "feishu-vc-stats", "mabang-process", "cookies-update",
            "daily", "schedule", "remote-wh", "shelve", "shelve-old"
        ]
        self.assertEqual(len(subcommands), 49)
        for subcmd in subcommands:
            with self.subTest(command=subcmd):
                res = self._run_cmd([subcmd, "--help"], expect_code=0)
                self.assertIn("usage: wb", res.stdout)

    def test_01b_vc_prefix_tail_format_mapping(self):
        """vendorCode 前缀正则须容忍「多变体序号」尾段（离线，纯正则断言）。

        背景：形如 `BCS-FZMG-334862442-2` / `BCS-ZZMX-191868961-15` 的卡（前缀后多一段 `-N`）
        此前不匹配 `config.VC_PREFIX_RE`（旧式要求「数字结尾 / 含斜杠」），导致
        `mapping.py` 前缀自动补录、`mapping_excel.py` 前缀兜底、`mapping_repo` 名称解析兜底
        **三处全部落空** → 每店恒定 55 条永久留在「未映射商品」（2026-09-21 实测：
        ZZMX 15 / FZMG 10 / GYDX 10 / MQXJ 10 / PPYD 10，三店完全同一批）。
        """
        import re
        from wb_ops import config

        rx = config.VC_PREFIX_RE
        # 兼容的合法格式（含新增的带序号尾段）
        valid = [
            ("BCS-CYQX-12345678", "CYQX"),                       # 标准
            ("BCS-CYQX-ozon-card-12345678", "CYQX"),             # 他人表 ozon-card 尾段
            ("BCS-QQNN-WRLINWI/1078999444", "QQNN"),             # 新供应商代码格式
            ("BCS-FZMG-334862442-2", "FZMG"),                    # ★ 多变体序号尾段（本次放宽）
            ("BCS-ZZMX-191868961-15", "ZZMX"),                   # ★
        ]
        for vc, expect in valid:
            with self.subTest(vc=vc):
                m = re.match(rx, vc)
                self.assertIsNotNone(m, f"应匹配成功: {vc}")
                self.assertEqual(m.group(1), expect)

        # 仍必须拒绝的非法格式（防止放宽后误收）
        invalid = [
            "BCS-qqnn-12345678",          # 小写前缀
            "BCS-1234-12345678",          # 数字前缀
            "BCS-QQN-12345678",           # 3 位前缀
            "BCS-QQNNN-12345678",         # 5 位前缀
            "BCS-QQNN-12345a",            # 数字尾段后跟字母
            "BCS-QQNN-12345-x",           # 序号尾段非数字
            "BCS-QQNN-",                  # 无尾段
            "ABC-QQNN-12345678",          # 非 BCS 开头
            "BCS-QQNN-abc-1",             # 首个尾段非数字
            "",
        ]
        for vc in invalid:
            with self.subTest(vc=vc):
                self.assertIsNone(re.match(rx, vc), f"应不匹配: {vc}")

    def test_01c_promo_goods_parse_offline(self):
        """promo-goods 离线解析断言（**不联网**）：查询串 / cmp 域请求头 / 供应商 UUID / 展平 / 本地反查。

        背景：cmp.wildberries.ru 广告推广接口 `GET /api/v1/adverts` 首次接入，
        被推广商品内嵌在 `content[].stocks.products[]`；抓包依据
        `api/网络请求/wb推广活动列表.har`（2026-09-23，status=[4,9,11]）。
        """
        from wb_ops.adapters import wb_ads_client as ads
        from wb_ops.services.discount import adverts

        # ① adverts 查询串：`[`/`]` 按 HAR 百分号编码、逗号保留
        url = ads.adverts_url(1, 100)
        self.assertIn("status=%5B4,9,11%5D", url)
        self.assertIn("bid_type=%5B1,2%5D", url)
        self.assertIn("type=%5B8,9%5D", url)
        self.assertIn("show_stocks=true", url)
        self.assertIn("page_number=1", url)
        self.assertIn("page_size=100", url)

        # ② cmp 域必需头：Authorization: Bearer <authorizev3>；x-supplierid 取自 cookie 的
        #    x-supplier-id-external（实测 credentials.json 三店均带，与 HAR 一致）
        shop = {"authorizev3": "JWT-TEST", "cookie": "a=1; x-supplier-id-external=UUID-XYZ; b=2"}
        h = ads.cmp_headers(shop)
        self.assertEqual(h["authorization"], "Bearer JWT-TEST")
        self.assertEqual(h["authorizev3"], "JWT-TEST")
        self.assertEqual(h["x-supplierid"], "UUID-XYZ")
        self.assertEqual(ads.supplier_uuid_from_cookie(shop), "UUID-XYZ")
        self.assertEqual(ads.supplier_uuid_from_cookie({"cookie": "a=1"}), "")  # 取不到留空 → 走兜底

        # ③ 展平：无商品的活动跳过；products_count>0 但明细为空 → 计入缺失清单（调用方打 [警告]）
        campaigns = [
            {"id": 40370920, "campaign_name": "c1", "status_id": 9, "payment_model": "cpc",
             "budget": 100, "create_date": "2026-09-23T06:01:11.811229+03:00", "products_count": 2,
             "stocks": {"products": [
                 {"nm": 1358933567, "name": "Переноска", "subject": {"name": "Переноски"},
                  "total_quantity_fbo": 0, "total_quantity_mp": 998},
                 {"nm": 1336777003, "name": "Пионы", "subject": {"id": 1156}, "total_quantity_fbo": 1}]}},
            {"id": 2, "campaign_name": "c2", "products_count": 3, "stocks": {"products": []}},
            {"id": 3, "campaign_name": "c3", "products_count": 0},
        ]
        flat = adverts.iter_campaign_products(campaigns)
        self.assertEqual(len(flat), 2)
        self.assertEqual([p["nm"] for _, p in flat], [1358933567, 1336777003])
        self.assertEqual(adverts.missing_detail_campaigns(campaigns), [2])

        # ④ --status 解析：显式给出走自定义，缺省回落后台默认视图 [4,9,11]
        class _Args:
            status = "9"
            no_cn = False
            page_size = 100
            limit = 0
            max_pages = 50

        self.assertEqual(adverts.build_options(_Args())["statuses"], (9,))
        _Args.status = ""
        opt = adverts.build_options(_Args())
        self.assertEqual(opt["statuses"], ads.STATUS_DEFAULT)
        self.assertTrue(opt["with_cn"])

        # ⑤ 本地真源反查：未收录 nmId → vc/cn 全空（调用方标注「本地真源未收录」，不联网核实）
        from wb_ops.storage.nm_resolver import NmResolver
        info = NmResolver(9352, with_cn=True).resolve(999999999999)
        self.assertEqual(info, {"vc": "", "cn": "", "src": ""})

    # ---------------- 2. 账号与基础数据 ----------------
    def test_02_shops_list(self):
        res = self._run_cmd(["shops"], expect_code=0)
        self.assertIn("9352", res.stdout)
        self.assertIn("袁州", res.stdout)

    def test_03_fetch_cached(self):
        res = self._run_cmd(["fetch", "--shop-id", "9352", "--no-sync"], expect_code=0)
        self.assertIn("9352", res.stdout)
        self.assertIn("shop9352_products_all.json", res.stdout)

    # ---------------- 3. 折扣与改价业务 ----------------
    def test_04_discount_dry_run(self):
        res = self._run_cmd(["discount", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("BCS-HAAJ-248364237", res.stdout)

    def test_04b_discount_below(self):
        res = self._run_cmd(["discount", "--threshold", "55", "--below", "40", "--target", "50",
                             "--shops", "9352", "--limit", "5"], expect_code=0)
        self.assertIn("或", res.stdout)
        self.assertIn("<40", res.stdout)

    def test_04c_discount_upload_two_phase(self):
        """WB 改折扣两阶段提交（离线 mock，不触网/不写平台）。

        依据抓包 `api/网络请求/wb批量修改折扣+降价提示.har`：`upload/task` 必须
        先 `checkChange=true` 预检（只回弹窗标记、无 id），再 `checkChange=false`
        真正提交（回 `data.id`）。旧实现 URL 写死 checkChange=true，导致只做了预检、
        taskId 恒为 None，平台侧根本没落库。
        """
        from unittest import mock
        from wb_ops.adapters import wb_client as wc

        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            if url.endswith("checkChange=true"):
                return {"data": {"priceModal": False, "quarantineModal": True}, "error": False, "errorText": ""}
            return {"data": {"id": 164469388, "alreadyExists": False}, "error": False, "errorText": ""}

        payload = [{"vendorCode": "BCS-QQNN-579331069", "nmID": 1579005260,
                    "discount": 49, "currencyIsoCode": "CNY"}]
        shop = {"shopId": 9352, "shopName": "袁州1", "cookie": "", "authorizev3": "t", "wb_seller_lk": "t"}
        client = wc.WBClient(shop, root_version="v1.113.3")

        with mock.patch.object(wc, "request", side_effect=fake_request):
            res = client.upload_batch_discount(payload)

        self.assertEqual([c[1].split("?")[-1] for c in calls],
                         ["checkChange=true", "checkChange=false"])
        self.assertEqual(calls[0][2], {"data": payload})
        self.assertEqual(calls[1][2], {"data": payload})
        self.assertTrue(res.success)
        self.assertEqual(res.task_id, 164469388)   # 有任务号 = 真落库
        self.assertTrue(res.quarantine_modal)
        self.assertFalse(res.price_modal)

        # precheck=False 时只提交一次，且仍必须带 checkChange=false
        calls.clear()
        with mock.patch.object(wc, "request", side_effect=fake_request):
            res2 = client.upload_batch_discount(payload, precheck=False)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith("checkChange=false"))
        self.assertEqual(res2.task_id, 164469388)
        self.assertFalse(res2.quarantine_modal)

    def test_05_discount_aliases(self):
        res1 = self._run_cmd(["discount-wb", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        res2 = self._run_cmd(["discount-scan", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("BCS-HAAJ-248364237", res1.stdout)
        self.assertIn("BCS-HAAJ-248364237", res2.stdout)

    def test_06_discount_bcs_dry_run(self):
        res = self._run_cmd(["discount-bcs", "--limit", "1", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_07_promo_apply_dry_run(self):
        res = self._run_cmd(["promo-apply", "--shops", "9352", "--days", "1"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_08_price_review_dry_run(self):
        res = self._run_cmd(["price-review", "--limit", "1", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    # ---------------- 4. Ops 一键操作 ----------------
    def test_09_price_dry_run(self):
        """price 默认通道 = WB 原生 dp-api 批量（2026-09-24 起切换，registry 路由 price_wb.run）。"""
        res = self._run_cmd(["price", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("WB 原生 dp-api", res.stdout)

    def test_09b_price_auto_quarantine_apply(self):
        """改价自动审核：隔离区按 nmID 精确匹配 + 跨域走 discount_svc 门面（离线 mock，不触网）。

        回归护栏：`ops_executor._auto_review_shop` 曾直接引用 discount 域私有模块
        `price_review` 却从未 import —— NameError 被 except Exception 吞掉，表现为
        「[自动审核] 店铺 X 失败: name 'price_review' is not defined」，自动审核**静默失效**
        （2026-09-21 实测）。现改为经 `discount_svc.apply_new_prices_by_nmids` 门面调用
        （REUSE_GUIDE 铁律 3：跨域只能经 `*_svc.py` 门面）。

        ⚠ 用例名刻意不含 review 字样：run_tests 选择器按「归一化子串 + 最长匹配」分派，
        `review` 与 `price` 等长时会把用例判给排在前面的 `review`，导致 `--cmd price` 漏测。
        """
        from unittest import mock
        from wb_ops.adapters import wb_client as wc
        from wb_ops.services.discount import price_review
        from wb_ops.services.discount_svc import discount_svc
        from wb_ops.services.replicate import ops_executor as oe

        quarantined = [{"id": 111, "nmID": 1001, "oldPrice": 198, "newPrice": 115},
                       {"id": 222, "nmID": 1002, "oldPrice": 158, "newPrice": 95},
                       {"id": 333, "nmID": 9999, "oldPrice": 100, "newPrice": 70}]  # 历史遗留待审
        posted = {}

        def fake_apply(session, ids):
            posted["ids"] = ids
            return {"error": False, "errorText": ""}

        shop = {"shopId": 9352, "shopName": "袁州1", "cookie": "",
                "authorizev3": "t", "wb_seller_lk": "t"}

        with mock.patch.object(wc, "make_session", return_value=object()), \
                mock.patch.object(price_review, "fetch_all_quarantine", return_value=quarantined), \
                mock.patch.object(price_review, "apply_prices", side_effect=fake_apply):

            # ① 门面：只审核 nm_ids 命中的待审项，不误审历史遗留（9999）
            r = discount_svc.apply_new_prices_by_nmids(shop, [1001, 1002])
            self.assertEqual(r["matched"], 2)
            self.assertEqual(r["applied"], 2)
            self.assertEqual(posted["ids"], [111, 222])

            # ② 调用方：改价后自动审核（原先此处恒 NameError → 返回 0，价格不生效）
            posted.clear()
            items = [{"nmID": 1001, "vc": "BCS-AAAA-1", "price": 115, "cur_price": 198.0},
                     {"nmID": 1002, "vc": "BCS-AAAA-2", "price": 95, "cur_price": 158.0},
                     {"nmID": 7777, "vc": "BCS-AAAA-3", "price": 500, "cur_price": 510.0}]  # 非 30-49.9%
            fake_cred = mock.Mock()
            fake_cred.wb_shop.return_value = shop
            with mock.patch.object(oe.credentials, "get", return_value=fake_cred):
                applied = oe._auto_review_shop(9352, items)
            self.assertEqual(applied, 2)          # 只审降价落 30-49.9% 的 2 个
            self.assertEqual(posted["ids"], [111, 222])

            # ③ 隔离区尚未生成（改价后平台异步入审查）→ 不报错、返回 0
            posted.clear()
            with mock.patch.object(price_review, "fetch_all_quarantine", return_value=[]):
                fake_cred2 = mock.Mock()
                fake_cred2.wb_shop.return_value = shop
                with mock.patch.object(oe.credentials, "get", return_value=fake_cred2):
                    self.assertEqual(oe._auto_review_shop(9352, items), 0)
            self.assertEqual(posted, {})

    def test_10_stock_dry_run(self):
        """stock 默认通道 = WB 原生在线接口（2026-09-24 起切换，registry 路由 stock_wb.run）。"""
        res = self._run_cmd(["stock", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("WB 原生在线接口", res.stdout)

    def test_09c_price_wb_dry_run(self):
        """price-wb dry-run：WB 原生 dp-api 通道（显式点名，不写平台）。
        用例名含归一化子串 pricewb → --cmd price-wb 精确命中；--cmd price 不会误捞。"""
        res = self._run_cmd(["price-wb", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("BCS-HAAJ-248364237", res.stdout)
        self.assertIn("WB 原生 dp-api", res.stdout)

    def test_09d_price_bcs_dry_run(self):
        """price-bcs 备选通道：BCS price/batch（dry-run 预览，无「WB 原生」字样）。"""
        res = self._run_cmd(["price-bcs", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertNotIn("WB 原生 dp-api", res.stdout)

    def test_09e_price_wb_offline(self):
        """price_wb 离线断言（**不触网、不写平台**）。

        依据 HAR `api/网络请求/wb在线批量修改价格.har`：
        - 提交条目 {"vendorCode","nmID","price","currencyIsoCode":"CNY"}（只改价无 discount 字段）
        - checkChange=true 预检（可能返回 priceModal/quarantineModal）→ checkChange=false 落库
          =「自动确认提交」（用户确认的语义，与改折扣两阶段同机制）
        """
        from unittest import mock
        from argparse import Namespace
        from wb_ops.adapters import wb_client as wc
        from wb_ops.services.replicate import price_wb

        shop = {"shopId": 9352, "shopName": "袁州1", "cookie": "",
                "authorizev3": "t", "wb_seller_lk": "t"}

        # ① 条目映射：只改价（无 discount 字段）/ --discount 显式给出（含 0）才带
        it = {"vc": "BCS-X-1", "nmID": 123, "price": 150, "cur_price": 198.0,
              "orig_zero": False, "discount": None, "clubDiscount": None, "cn": "测试"}
        p0 = price_wb._build_payload(it, None)
        self.assertEqual(p0, {"vendorCode": "BCS-X-1", "nmID": 123,
                              "price": 150, "currencyIsoCode": "CNY"})
        self.assertEqual(price_wb._build_payload(it, 30)["discount"], 30)
        self.assertEqual(price_wb._build_payload(it, 0)["discount"], 0)   # 显式 0 也照传

        # ② exec_shop：checkChange 两阶段 + 预检 modal=true 仍自动确认落库 + 分批
        calls = []

        def fake_request(session, method, url, **kwargs):
            calls.append((url.split("?")[-1], kwargs.get("json")))
            if url.endswith("checkChange=true"):
                return {"data": {"priceModal": True, "quarantineModal": True}, "error": False, "errorText": ""}
            return {"data": {"id": 166628902, "alreadyExists": False}, "error": False, "errorText": ""}

        items = [{"vc": f"BCS-WB-{i}", "nmID": 1000 + i, "price": 150, "cur_price": 198.0,
                  "orig_zero": False, "discount": None, "clubDiscount": None, "cn": "测试"}
                 for i in range(4)]
        plan = {"shopId": 9352, "items": items}
        pargs = Namespace(chunk=2, interval=0, discount=None)
        client = wc.WBClient(shop, root_version="v1.113.3")
        results, ts = [], "2026-09-24 12:00:00"
        with mock.patch.object(wc, "request", side_effect=fake_request):
            ok, fail = price_wb.exec_shop(client, plan, pargs, results, ts)
        self.assertEqual((ok, fail), (4, 0))
        self.assertEqual([c[0] for c in calls],
                         ["checkChange=true", "checkChange=false", "checkChange=true", "checkChange=false"])
        self.assertEqual(calls[0][1], {"data": [
            {"vendorCode": "BCS-WB-0", "nmID": 1000, "price": 150, "currencyIsoCode": "CNY"},
            {"vendorCode": "BCS-WB-1", "nmID": 1001, "price": 150, "currencyIsoCode": "CNY"}]})
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r[2] == "price_wb" for r in results))

        # ③ apply_prices 门面：chunk 分批 + alreadyExists 计成功 + task_ids 收集
        def fake_request_exists(session, method, url, **kwargs):
            if url.endswith("checkChange=true"):
                return {"data": {"priceModal": False, "quarantineModal": False}, "error": False, "errorText": ""}
            return {"data": {"id": 166628902, "alreadyExists": True}, "error": False, "errorText": ""}

        witems = [{"vendorCode": f"BCS-WB-{i}", "nmID": 2000 + i, "price": 88} for i in range(3)]
        calls.clear()
        with mock.patch.object(wc, "request", side_effect=fake_request_exists):
            r = price_wb.apply_prices(shop, witems, root_version="v1.113.3", chunk=2, interval=0)
        self.assertEqual((r["ok"], r["fail"]), (3, 0))
        self.assertEqual(r["task_ids"], [166628902, 166628902])
        self.assertEqual(len(r["details"]), 3)
        self.assertTrue(all(d["ok"] for d in r["details"]))

        # ④ ≤原价/2 预拦截：price=99 ≤ cur/2=99 → 剔除不提交
        bad_items = [{"vc": "BCS-WB-9", "nmID": 9999, "price": 99, "cur_price": 198.0,
                      "orig_zero": False, "discount": None, "clubDiscount": None, "cn": "测试"}]
        plan_bad = {"shopId": 9352, "items": bad_items}
        calls.clear()
        with mock.patch.object(wc, "request", side_effect=fake_request):
            ok2, fail2 = price_wb.exec_shop(client, plan_bad, pargs, [], ts)
        self.assertEqual((ok2, fail2), (0, 0))
        self.assertEqual(calls, [])          # 全部被拦截 → 不发任何请求

    def test_10b_stock_bcs_dry_run(self):
        """stock-bcs 备选通道：BCS stock/batchSetByChrtIdsBatch（dry-run 预览）。"""
        res = self._run_cmd(["stock-bcs", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_10c_stock_wb_dry_run(self):
        """stock-wb dry-run：WB 原生在线接口通道（快照解析，不写平台）。
        用例名含归一化子串 stockwb → --cmd stock-wb 精确命中；
        --cmd stock 时最长匹配不会误捞本用例（归一化 stockwb ⊃ stock 反向不成立）。"""
        res = self._run_cmd(["stock-wb", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("BCS-HAAJ-248364237", res.stdout)
        self.assertIn("WB 原生在线接口", res.stdout)

    def test_10d_stock_wb_offline(self):
        """stock-wb 离线断言（**不触网、不写平台**）：

        依据 HAR `api/网络请求/wb在线加载修改库存.har`：
        - 修改端点 POST .../api/v3/portal/stocks/{warehouseId}，body {"data":[{chrtId,amount}]}
        - 数组批量（探针 _scratch/probe_stock_wb_capacity.py 实测 1000 条 error=false）
        - 查询端点 GET 同域 portal/stocks?order=asc[&stores=][&search=]（data.next 游标分页）
        """
        from unittest import mock
        from wb_ops.adapters import wb_stock_client as wsc
        from wb_ops.services.replicate import stock_wb

        shop = {"shopId": 9352, "shopName": "袁州1", "cookie": "",
                "authorizev3": "t", "wb_seller_lk": "t"}

        # ① post_stocks：URL 以 warehouseId 结尾、body 结构与 allow_400_json 默认开启
        posts = []

        def fake_request_post(session, url, payload, allow_400_json=False):
            posts.append((url, payload, allow_400_json))
            return {"error": False, "errorText": "", "data": {}}

        with mock.patch.object(wsc.wb_api, "request_post", side_effect=fake_request_post):
            r = wsc.post_stocks(object(), 1929635, [{"chrtId": 2102520529, "amount": 30}])
        self.assertTrue(posts[0][0].endswith("/api/v3/portal/stocks/1929635"))
        self.assertEqual(posts[0][1], {"data": [{"chrtId": 2102520529, "amount": 30}]})
        self.assertTrue(posts[0][2])          # WB 400 也带 JSON 错误体 → allow_400_json 默认 True
        self.assertFalse(r["error"])

        # ② set_stock 门面：201 条按 chunk=200 分 200+1 两批；明细 201 条全成功
        items = [{"chrtId": 1000 + i, "amount": i} for i in range(201)]
        posts.clear()
        with mock.patch.object(wsc.wb_api, "request_post", side_effect=fake_request_post), \
                mock.patch.object(wsc.wb_api, "make_session", return_value=object()):
            r2 = stock_wb.set_stock(shop, 1929635, items, chunk=200, interval=0)
        self.assertEqual([len(p[1]["data"]) for p in posts], [200, 1])
        self.assertEqual((r2["ok"], r2["fail"]), (201, 0))
        self.assertEqual(len(r2["details"]), 201)
        self.assertTrue(all(d["ok"] for d in r2["details"]))

        # ③ chunk 上限夹取：chunk=5000 传入门面 → 实际按 1000 分批（1001 条 → 1000+1）
        items_1001 = [{"chrtId": 2000 + i, "amount": i} for i in range(1001)]
        posts.clear()
        with mock.patch.object(wsc.wb_api, "request_post", side_effect=fake_request_post), \
                mock.patch.object(wsc.wb_api, "make_session", return_value=object()):
            r3 = stock_wb.set_stock(shop, 1929635, items_1001, chunk=5000, interval=0)
        self.assertEqual([len(p[1]["data"]) for p in posts], [1000, 1])
        self.assertEqual(r3["ok"], 1001)

        # ④ 失败路径：error=true → 全部计失败，msg 取 errorText
        def fake_post_fail(session, url, payload, allow_400_json=False):
            return {"error": True, "errorText": "Недостаточно прав", "data": {}}

        with mock.patch.object(wsc.wb_api, "request_post", side_effect=fake_post_fail), \
                mock.patch.object(wsc.wb_api, "make_session", return_value=object()):
            r4 = stock_wb.set_stock(shop, 1929635, items[:3], chunk=200, interval=0)
        self.assertEqual((r4["ok"], r4["fail"]), (0, 3))
        self.assertEqual(r4["details"][0]["msg"], "Недостаточно прав")

        # ⑤ get_stocks：stores/search 参数透传 + data.next 游标翻页 + 空游标终止
        pages = [
            {"data": {"next": "CUR1",
                      "stocks": [{"article": "BCS-A-1", "chrtId": 11, "amount": 1}]}},
            {"data": {"next": "",
                      "stocks": [{"article": "BCS-B-2", "chrtId": 22, "amount": 0}]}},
        ]
        gets = []

        def fake_request(session, method, url, **kwargs):
            gets.append(kwargs.get("params") or {})
            return pages[len(gets) - 1]

        with mock.patch.object(wsc.wb_api, "request", side_effect=fake_request):
            got = wsc.get_stocks(object(), store_id=1929635, search="BCS-A-1")
        self.assertEqual([g["chrtId"] for g in got], [11, 22])
        self.assertEqual(gets[0].get("stores"), 1929635)
        self.assertEqual(gets[0].get("search"), "BCS-A-1")
        self.assertNotIn("next", gets[0])     # 首页不带 next
        self.assertEqual(gets[1].get("next"), "CUR1")
        self.assertEqual(gets[1].get("search"), "BCS-A-1")  # search 每页透传

    def test_11_trash_dry_run(self):
        res = self._run_cmd(["trash", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    # ---------------- 5. 商品搬家与清理 ----------------
    def test_12_replicate_dry_run(self):
        res = self._run_cmd(["replicate", "--vc", "BCS-HAAJ-248364237", "--limit", "1"], expect_code=0)
        self.assertIn("vendorCode", res.stdout)

    def test_13_dimension_dry_run(self):
        res = self._run_cmd(["dimension", "--vc", "BCS-HAAJ-248364237", "--shops", "9352", "--dims", "8*14*26/0.3"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_14_dims_check(self):
        res = self._run_cmd(["dims-check", "--limit", "1", "--shops", "9352"], expect_code=0)
        self.assertIn("袁州1", res.stdout)

    def test_15_banned_dry_run(self):
        res = self._run_cmd(["banned", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_16_clean_dry_run(self):
        res = self._run_cmd(["clean", "--target", "basket", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_17_remote_wh_dry_run(self):
        res = self._run_cmd(["remote-wh", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    # ---------------- 6. 映射与工作台 ----------------
    def test_18_shops_mapping(self):
        res = self._run_cmd(["shops-mapping", "--shop-id", "9352"], expect_code=0)
        self.assertIn("shop_9352", res.stdout)

    def test_19_mapping_check(self):
        res = self._run_cmd(["mapping-check", "--tol", "5"], expect_code=0)
        self.assertIn("映射表核查工作台.html", res.stdout)

    def test_20_mismatch_check(self):
        res = self._run_cmd(["mismatch-check", "--days", "1"], expect_code=0)
        self.assertIn("货不对板筛查工作台.html", res.stdout)

    def test_21_review(self):
        res = self._run_cmd(["review"], expect_code=0)
        self.assertIn("待审核工作台已生成", res.stdout)

    # ---------------- 7. 订单与马帮 ----------------
    def test_22_orders(self):
        res = self._run_cmd(["orders", "--no-sync", "--days", "1", "--shops", "9352"], expect_code=0)
        self.assertIn("订单", res.stdout)

    def test_23_mabang_orders_dry_run(self):
        res = self._run_cmd(["mabang-orders", "--days", "1"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_24_mabang_forecast_check(self):
        res = self._run_cmd(["mabang-forecast", "--check"], expect_code=0)
        self.assertIn("预报批次列表", res.stdout)

    def test_25_mabang_process_dry_run(self):
        res = self._run_cmd(["mabang-process", "--days", "1"], expect_code=0)
        self.assertIn("步骤", res.stdout)

    # ---------------- 8. 客服与监控 ----------------
    def test_26_questions(self):
        res = self._run_cmd(["questions", "--no-detail", "--shops", "9352"], expect_code=0)
        self.assertIn("未处理提问", res.stdout)

    def test_27_questions_watch_once(self):
        res = self._run_cmd(["questions-watch", "--once", "--shops", "9352"], expect_code=0)
        self.assertIn("--once", res.stdout)

    def test_27b_appeals(self):
        res = self._run_cmd(["appeals", "--no-cn", "--shops", "9352", "--limit", "1"], expect_code=0)
        self.assertIn("店铺 1 个", res.stdout)
        self.assertIn("供应商代码", res.stdout)

    # ---------------- 9. 别名路由与并发安全 ----------------
    def test_28_cli_aliases(self):
        res1 = self._run_cmd(["mapping-merge", "--help"], expect_code=0)
        self.assertIn("merge", res1.stdout)
        res2 = self._run_cmd(["mapping-sync", "--help"], expect_code=0)
        self.assertIn("shops-mapping", res2.stdout)
        res3 = self._run_cmd(["order-pipeline", "--help"], expect_code=0)
        self.assertIn("mabang-process", res3.stdout)

    def test_29_mapping_rename(self):
        from wb_ops.storage.mapping_repo import MappingRepository
        try:
            res = self._run_cmd(["mapping-rename", "--vc", "BCS-TEST-TEST12345", "--cn", "单元测试商品-纠偏", "--reason", "自动化验证"], expect_code=0)
            self.assertIn("BCS-TEST-TEST12345", res.stdout)
            self.assertIn("单元测试商品-纠偏", res.stdout)
            # 2026-09-21：纠偏须同步写入全局已知池（vc_known.json），
            # 避免「前缀码匹配不到的怪 vc」其名字仅存于 vc_override.json 而随文件丢失失效
            self.assertIn("已知池同步", res.stdout)
            known = MappingRepository.load_vc_known()
            self.assertEqual((known.get("BCS-TEST-TEST12345") or {}).get("cn"), "单元测试商品-纠偏")
        finally:
            try:
                ov = MappingRepository.load_vc_override()
                if "BCS-TEST-TEST12345" in ov:
                    del ov["BCS-TEST-TEST12345"]
                    MappingRepository.save_vc_override(ov)
                kn = MappingRepository.load_vc_known()
                if "BCS-TEST-TEST12345" in kn:
                    del kn["BCS-TEST-TEST12345"]
                    MappingRepository.save_vc_known(kn)
            except Exception:
                pass

    def test_30_safe_io_concurrency(self):
        from wb_ops.framework.safe_io import FileLock, atomic_dump_json, safe_load_json
        test_path = os.path.join(BASE_DIR, "data", "state", ".test_ci_lock.json")
        try:
            with FileLock(test_path, timeout=5.0):
                # 测试重入锁与原子读写事务
                atomic_dump_json(test_path, {"test_run": True, "count": 1}, use_lock=True)
                d = safe_load_json(test_path, use_lock=True)
                self.assertEqual(d.get("count"), 1)
        finally:
            if os.path.exists(test_path):
                os.remove(test_path)


    # ---------------- 10. 补测：原缺测命令的只读路径 ----------------
    # 说明：本段全部为「只读/无副作用」路径 —— 参数校验、缺失文件报错、dry-run 预览、只读预览。
    #      绝不触发任何写平台/写飞书/写系统的动作（所有写操作命令均不带 --apply）。
    def test_31_import_shelve_argcheck(self):
        # 缺位置参数 → argparse 直接报错退出（不进入业务逻辑，不触发 ensure_snapshots/网络）
        res = self._run_cmd(["import-shelve"], expect_code=2)
        self.assertIn("usage", (res.stdout + res.stderr).lower())

    def test_32_discount_aliases_below(self):
        # 两个别名命令走 dry-run，并覆盖新增的双侧区间参数（--below 走 WB 升序接口）
        for alias in ("discount-wb", "discount-scan"):
            with self.subTest(alias=alias):
                res = self._run_cmd([alias, "--threshold", "55", "--below", "40", "--target", "50",
                                     "--shops", "9352", "--limit", "1"], expect_code=0)
                self.assertIn("dry-run", res.stdout)
                self.assertIn("<40", res.stdout)

    def test_33_ai_test_missing_qa(self):
        # 数据集文件不存在 → 明确报错退出（不调用 LLM，不产生费用）
        res = self._run_cmd(["ai-test", "--qa", os.path.join(BASE_DIR, "_scratch", "__no_such_qa__.json")],
                            expect_code=1)
        self.assertIn("[错误]", res.stdout + res.stderr)

    def test_34_feishu_register_dry_run(self):
        # scope=latest：默认合并「待处理订单」数据源（覆盖只匹配未进预报/上传/交运的单）
        res = self._run_cmd(["feishu-register", "--scope", "latest"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("[合并]", res.stdout)
        self.assertIn("真排除", res.stdout)

    def test_34b_feishu_register_no_pending(self):
        # --no-pending：关闭合并（只按 orderalllist 登记）
        res = self._run_cmd(["feishu-register", "--scope", "latest", "--no-pending"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertNotIn("[合并]", res.stdout)

    def test_35_mabang_stock_register_dry_run(self):
        res = self._run_cmd(["mabang-stock-register"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_36_mabang_stock_daily_end_only(self):
        # --end 单独使用必须报错中止（避免静默删列）
        res = self._run_cmd(["mabang-stock-daily", "--end", "2026-09-20"], expect_code=1)
        self.assertIn("--end", res.stdout)

    def test_36b_feishu_vc_stats_offline(self):
        """feishu-vc-stats 离线断言（**不联网、不调 lark-cli**）：字段解析 / 窗口 / 跨店合并 / 降序 / 前缀。

        背景：飞书「订单登记」表的 vendorCode 载体是 `BCS编号` 列（表内无「供应商代码」列）；
        同一 vendorCode 在各店 wb编号 不同，但统计须**跨店合并**到同一条（用户口径 2026-09-23）。
        """
        from datetime import datetime, timedelta

        from wb_ops.services.order import feishu_vc_stats as st

        # ① 字段解析：日期取前 10 位、店铺(list 多选)、订单量 number、BCS编号
        row = st.parse_row({
            "日期": "2026-09-22T03:26:00.000+08:00",
            "订单编号": "419915682",
            "店铺": ["袁州1"],
            "BCS编号": "BCS-JPAV-163906301",
            "商品中文名": "毛球修剪器",
            "订单量": 3,
        })
        self.assertEqual(row["日期"], "2026-09-22")
        self.assertEqual(row["vc"], "BCS-JPAV-163906301")
        self.assertEqual(row["cn"], "毛球修剪器")
        self.assertEqual(row["shops"], ["袁州1"])
        self.assertEqual(row["qty"], 3)
        self.assertEqual(row["order_id"], "419915682")
        # 缺字段 / 空店铺 容错
        empty = st.parse_row({})
        self.assertEqual((empty["日期"], empty["vc"], empty["shops"], empty["qty"]), ("", "", [], 1))

        # ② 窗口：闭区间 + 无日期不命中
        self.assertTrue(st.in_window({"日期": "2026-09-22"}, "2026-09-22", "2026-09-22"))
        self.assertTrue(st.in_window({"日期": "2026-09-17"}, "2026-09-17", "2026-09-23"))
        self.assertFalse(st.in_window({"日期": "2026-09-16"}, "2026-09-17", "2026-09-23"))
        self.assertFalse(st.in_window({"日期": ""}, "2026-09-17", "2026-09-23"))

        # ③ 窗口推导：days 含今天；date 单天；仅 begin 按单天；仅 end 报错
        b, e = st.resolve_window(days=7)
        self.assertEqual(e, time.strftime("%Y-%m-%d"))
        self.assertEqual(datetime.strptime(e, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d"),
                         timedelta(days=6))
        self.assertEqual(st.resolve_window(date="2026-9-5"), ("2026-09-05", "2026-09-05"))
        self.assertEqual(st.resolve_window(begin="2026-09-20"), ("2026-09-20", "2026-09-20"))
        self.assertEqual(st.resolve_window(begin="2026-09-02", end="2026-09-05"), ("2026-09-02", "2026-09-05"))
        with self.assertRaises(ValueError):
            st.resolve_window(end="2026-09-22")

        # ④ 跨店合并 + 单数降序 + 同数按 key 升序 + 件数累加
        rows = [
            {"日期": "2026-09-22", "vc": "BCS-AAAA-1", "cn": "甲", "shops": ["袁州1"], "qty": 2, "order_id": "o1"},
            {"日期": "2026-09-22", "vc": "BCS-AAAA-1", "cn": "甲", "shops": ["袁州3"], "qty": 1, "order_id": "o2"},
            {"日期": "2026-09-22", "vc": "BCS-BBBB-2", "cn": "乙", "shops": ["袁州2"], "qty": 1, "order_id": "o3"},
            {"日期": "2026-09-22", "vc": "BCS-CCCC-3", "cn": "丙", "shops": ["袁州1"], "qty": 1, "order_id": "o4"},
            {"日期": "2026-09-22", "vc": "", "cn": "", "shops": ["袁州1"], "qty": 1, "order_id": "o5"},
        ]
        agg = st.aggregate(rows)
        self.assertEqual([(r["key"], r["orders"], r["qty"]) for r in agg],
                         [("BCS-AAAA-1", 2, 3), ("BCS-BBBB-2", 1, 1), ("BCS-CCCC-3", 1, 1), (st.NO_VC_KEY, 1, 1)])
        self.assertEqual([r["rank"] for r in agg], [1, 2, 3, 4])
        # 未归类桶恒排最后（不然 '(' 码位小于 'B' 会插到真实 vendorCode 前面）
        self.assertTrue(str(agg[-1]["key"]).startswith("("))
        # 跨店合并：同一 vc 在袁州1/袁州3 的店铺分布已合并（各 1 单 → 按单数降序、同数按名升序）
        self.assertEqual(agg[0]["shops"], ["袁州1", "袁州3"])
        self.assertEqual(agg[0]["cn"], "甲")
        # 无 BCS编号 的记录不静默丢弃，单独成桶
        self.assertEqual([r["key"] for r in agg if r["key"] == st.NO_VC_KEY], [st.NO_VC_KEY])

        # ⑤ 前缀码聚合视角
        pre = st.aggregate(rows, by_prefix=True)
        self.assertEqual([(r["key"], r["orders"]) for r in pre], [("AAAA", 2), ("BBBB", 1), ("CCCC", 1), (st.NO_PREFIX_KEY, 1)])

        # ⑥ 店铺过滤：空集 = 不过滤；否则取交集
        r_shop = {"日期": "2026-09-22", "vc": "x", "shops": ["袁州2"]}
        self.assertTrue(st.match_shops(r_shop, None))
        self.assertTrue(st.match_shops(r_shop, set()))
        self.assertFalse(st.match_shops(r_shop, {"袁州1", "袁州3"}))
        self.assertTrue(st.match_shops(r_shop, {"袁州2"}))

        # ⑦ 店铺标签解析：9352 → 袁州1（数字经 credentials 反查）；未知数字保留原值
        self.assertEqual(st.resolve_shop_labels("9352"), {"袁州1"})
        self.assertEqual(st.resolve_shop_labels("袁州1,袁州3"), {"袁州1", "袁州3"})
        self.assertEqual(st.resolve_shop_labels(["9353", "璧山1"]), {"袁州2", "璧山1"})
        self.assertEqual(st.resolve_shop_labels(""), set())

    def test_37_cookies_update_missing_file(self):
        res = self._run_cmd(["cookies-update", "__no_such_cookies__.md"], expect_code=1)
        self.assertIn("[错误]", res.stdout + res.stderr)

    def test_38_daily_safe_scope(self):
        # 用不存在的店铺 ID 限定，使各步骤「无匹配店铺」空转（不产生任何写操作）；
        # daily 的输出落当日日志 data/logs/daily_YYYYMMDD.log，故断言日志内容
        res = self._run_cmd(["daily", "check", "--shops", "0"], expect_code=0)
        self.assertEqual(res.stdout.strip(), "")  # 步骤输出被重定向进日志
        log_path = os.path.join(BASE_DIR, "data", "logs", "daily_" + time.strftime("%Y%m%d") + ".log")
        self.assertTrue(os.path.exists(log_path), f"未生成当日日志: {log_path}")
        with open(log_path, encoding="utf-8", errors="replace") as f:
            blob = f.read()
        self.assertIn("[改折扣]", blob)
        self.assertIn("店铺 0 个", blob)

    def test_39_schedule_plan(self):
        res = self._run_cmd(["schedule", "--plan"], expect_code=0)
        self.assertIn("[dry-run]", res.stdout)
        self.assertIn("WB_Daily_Morning", res.stdout)
        self.assertIn("schtasks", res.stdout)

    def test_40_shelve_dry_run(self):
        # 支持单品/多品、指定前缀码与尺寸重量等灵活参数
        res = self._run_cmd(["shelve", "248364237", "388854754", "--price", "59", "--prefix", "ABCD", "--dims", "10*20*30/0.5"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("248364237", res.stdout)
        self.assertIn("388854754", res.stdout)

    def test_41_shelve_old_dry_run(self):
        # 支持指定任意自定义完整 vendorCode
        res = self._run_cmd(["shelve-old", "248364237", "--price", "59", "--vc", "BCS-CUSTOM-SPECIAL-12345", "--dims", "10*20*30/0.5"], expect_code=0)
        self.assertIn("dry-run", res.stdout)
        self.assertIn("BCS-CUSTOM-SPECIAL-12345", res.stdout)

    def test_42_shelve_backend_interchangeable(self):
        # 验证 replicate 与 import_shelve 可平滑无缝将 shelve_backend 切换为 shelve_old
        from wb_ops.services.replicate import replicate, import_shelve
        from wb_ops.services.replicate import shelve_new, shelve_old
        self.assertTrue(hasattr(shelve_new, "execute_shelve"))
        self.assertTrue(hasattr(shelve_old, "execute_shelve"))
        # 验证两者签名与关键参数完全对称
        import inspect
        sig_new = inspect.signature(shelve_new.execute_shelve)
        sig_old = inspect.signature(shelve_old.execute_shelve)
        self.assertEqual(list(sig_new.parameters.keys()), list(sig_old.parameters.keys()))

    def test_43_legacy_payload_images_subject_id(self):
        # 验证旧接口 build_legacy_payload 在无外部图片与合成 detail 时，仍能保证图片非空且 subjectId 提取正确
        from wb_ops.domain.models import ShelveItem
        from wb_ops.services.replicate.shelve_old import build_legacy_payload
        from wb_ops.services.replicate.shelve_common import fetch_card_json
        item = ShelveItem(
            nm_id=248364237,
            price=99.0,
            vendor_code="BCS-CUSTOM-PAYLOAD-TEST",
            length=10, width=20, height=30, weight=0.5
        )
        card = fetch_card_json(248364237) or {"imt_name": "测试商品", "imt_id": 1, "data": {"subject_id": 2290}}
        payload, err = build_legacy_payload(item, [9352], {9352: 1929635}, card, None)
        self.assertIsNone(err)
        self.assertIsNotNone(payload)
        sdata = payload["shopDatas"][0]
        self.assertTrue(len(sdata["images"]) > 0, "图片链接不得为空")
        self.assertTrue(len(sdata["mainImage"]) > 0, "主图链接不得为空")
        self.assertGreater(sdata["subjectId"], 0, "类目ID必须大于0")
        self.assertEqual(sdata["vendorCode"], "BCS-CUSTOM-PAYLOAD-TEST")

    def test_44_legacy_payload_missing_warehouse(self):
        # 验证仓库配置缺失时返回明确错误而非崩溃抛出 KeyError
        from wb_ops.domain.models import ShelveItem
        from wb_ops.services.replicate.shelve_old import build_legacy_payload
        item = ShelveItem(nm_id=248364237, price=99.0, vendor_code="BCS-TEST-VC")
        card = {"data": {"subject_id": 100}}
        payload, err = build_legacy_payload(item, [99999], {}, card, None)
        self.assertIsNone(payload)
        self.assertIn("缺少对应发货仓库配置", err)

    def test_45_shelve_old_csv_file_support(self):
        # 验证 shelve-old 直接读取订单筛选 CSV 文件并进行预览
        csv_file = r"C:\Users\Admin\Desktop\筛选非我店订单_20260917.csv"
        if os.path.exists(csv_file):
            res = self._run_cmd(["shelve-old", "--file", csv_file, "--shops", "9352,9353,9356"], expect_code=0)
            self.assertIn("dry-run", res.stdout)
            self.assertIn("1378756362", res.stdout)
            self.assertIn("BCS-DZJF-209792332", res.stdout)
            self.assertIn("原始VC", res.stdout)

    def test_46_promo_gap_offline(self):
        """promo-gap 离线差集/分组断言（**不联网、不调 lark-cli**）。

        口径（2026-09-23 与用户确认并**修正为「合计」**）：**销量判据 = 该供应商代码在目标店铺
        （默认袁州1/2/3）的 7 天单数「合计」**，不是单店单数（同一 vc 各店 wb编号 不同，但它是同一个商品）。
        正向 gap：合计 ≥ 阈值 且某目标店未推广 → 该店该补推；
        反向 waste：合计 < 阈值 → 这些店所有「在投(status=9)」活动里的推广都建议关闭；
        nm 本地真源反查不到 vc → 「无法判定」（不推断）；暂停(11)/未识别状态 → 仅计数不逐条列。
        """
        from wb_ops.services.discount import promo_gap as pg

        # ① 阈值解析
        self.assertEqual(pg.resolve_min(None), 4)
        self.assertEqual(pg.resolve_min(""), 4)
        self.assertEqual(pg.resolve_min("6"), 6)
        for bad in (0, -3, "abc"):
            with self.assertRaises(ValueError):
                pg.resolve_min(bad)

        # ② 店铺选择：店铺ID 与短名双认
        all_shops = [{"shopId": 9352, "shopName": "袁州1"}, {"shopId": 9353, "shopName": "袁州2"}]
        self.assertEqual([x[1] for x in pg.select_shops(all_shops, "9352")], [9352])
        self.assertEqual([x[2] for x in pg.select_shops(all_shops, "袁州2")], ["袁州2"])
        self.assertEqual(len(pg.select_shops(all_shops, "9352,袁州2")), 2)
        self.assertEqual(len(pg.select_shops(all_shops, "")), 2)

        rows = [
            # AAAA：袁州1×2 + 袁州2×2 → 合计 4（单店各 2 均 <4 ⇒ 旧「单店」口径会漏判）
            {"日期": "2026-09-22", "vc": "BCS-AAAA-1", "cn": "甲", "shops": ["袁州1"], "qty": 1, "order_id": "a1"},
            {"日期": "2026-09-22", "vc": "BCS-AAAA-1", "cn": "甲", "shops": ["袁州1"], "qty": 2, "order_id": "a2"},
            {"日期": "2026-09-22", "vc": "BCS-AAAA-1", "cn": "甲", "shops": ["袁州2"], "qty": 1, "order_id": "a3"},
            {"日期": "2026-09-22", "vc": "BCS-AAAA-1", "cn": "甲", "shops": ["袁州2"], "qty": 1, "order_id": "a4"},
            # BBBB：袁州1×4 → 合计 4
            {"日期": "2026-09-22", "vc": "BCS-BBBB-2", "cn": "乙", "shops": ["袁州1"], "qty": 1, "order_id": "b1"},
            {"日期": "2026-09-22", "vc": "BCS-BBBB-2", "cn": "乙", "shops": ["袁州1"], "qty": 1, "order_id": "b2"},
            {"日期": "2026-09-22", "vc": "BCS-BBBB-2", "cn": "乙", "shops": ["袁州1"], "qty": 1, "order_id": "b3"},
            {"日期": "2026-09-22", "vc": "BCS-BBBB-2", "cn": "乙", "shops": ["袁州1"], "qty": 1, "order_id": "b4"},
            # CCCC：袁州1×1 → 合计 1
            {"日期": "2026-09-22", "vc": "BCS-CCCC-3", "cn": "丙", "shops": ["袁州1"], "qty": 1, "order_id": "c1"},
            # EEEE：店铺多选 袁州1+袁州3 ×4 → 合计只计一次=4（不是 8），拆分各计 4
            {"日期": "2026-09-22", "vc": "BCS-EEEE-4", "cn": "丁", "shops": ["袁州1", "袁州3"], "qty": 1, "order_id": "e1"},
            {"日期": "2026-09-22", "vc": "BCS-EEEE-4", "cn": "丁", "shops": ["袁州1", "袁州3"], "qty": 1, "order_id": "e2"},
            {"日期": "2026-09-22", "vc": "BCS-EEEE-4", "cn": "丁", "shops": ["袁州1", "袁州3"], "qty": 1, "order_id": "e3"},
            {"日期": "2026-09-22", "vc": "BCS-EEEE-4", "cn": "丁", "shops": ["袁州1", "袁州3"], "qty": 1, "order_id": "e4"},
            # FFFF：只在非目标店（璧山1）→ 目标店口径下不计
            {"日期": "2026-09-22", "vc": "BCS-FFFF-6", "cn": "己", "shops": ["璧山1"], "qty": 1, "order_id": "f1"},
            # 无 BCS编号
            {"日期": "2026-09-22", "vc": "", "cn": "", "shops": ["袁州1"], "qty": 1, "order_id": "n1"},
        ]

        # ③ 跨店合计 vs 各店拆分（★ 本次口径修正的核心）
        by_vc, by_shop, no_vc = pg.aggregate_orders(rows, {"袁州1", "袁州2", "袁州3"})
        self.assertEqual(by_vc["BCS-AAAA-1"]["orders"], 4)
        self.assertEqual(by_vc["BCS-AAAA-1"]["qty"], 5)
        self.assertEqual(by_shop["BCS-AAAA-1"]["袁州1"]["orders"], 2)
        self.assertEqual(by_shop["BCS-AAAA-1"]["袁州2"]["orders"], 2)
        self.assertEqual(by_vc["BCS-EEEE-4"]["orders"], 4)          # 多店铺记录合计只计一次
        self.assertEqual(by_shop["BCS-EEEE-4"]["袁州1"]["orders"], 4)
        self.assertEqual(by_shop["BCS-EEEE-4"]["袁州3"]["orders"], 4)
        self.assertNotIn("BCS-FFFF-6", by_vc)                        # 非目标店不计
        self.assertEqual(no_vc, 1)
        self.assertEqual(pg.shop_split_text(by_shop["BCS-AAAA-1"]), "袁州1=2,袁州2=2")
        by1, _ = pg.shop_rows_by_vc(rows, "袁州1")                    # 单店视角包装仍可用
        self.assertEqual(by1["BCS-BBBB-2"]["orders"], 4)

        # ④ 正向 gap：合计≥4 且该目标店未推广 → 该店缺口
        targets2 = [({}, 9352, "袁州1"), ({}, 9353, "袁州2")]
        adverted = {9352: {"BCS-BBBB-2"}, 9353: set()}
        snaps = {9352: {"BCS-AAAA-1": {}, "BCS-EEEE-4": {"nmId": 444}},
                 9353: {"BCS-AAAA-1": {"nmID": 111}}}
        g = pg.build_gap(targets2, rows, adverted, snaps, min_orders=4)
        self.assertEqual([(x["vc"], x["shop_id"]) for x in g["gap_listed"]],
                         [("BCS-AAAA-1", 9352), ("BCS-AAAA-1", 9353), ("BCS-EEEE-4", 9352)])
        self.assertEqual([(x["vc"], x["shop_id"]) for x in g["gap_unlisted"]],
                         [("BCS-BBBB-2", 9353), ("BCS-EEEE-4", 9353)])
        self.assertEqual(g["gap_listed"][0]["orders"], 4)            # 合计单数
        self.assertEqual(g["gap_listed"][0]["shop_orders"], 2)       # 该店单数
        self.assertEqual(g["gap_listed"][0]["shop_split"], "袁州1=2,袁州2=2")
        self.assertEqual(g["gap_listed"][1]["nm_id"], 111)           # nmID 键兼容
        self.assertEqual(g["gap_listed"][2]["nm_id"], 444)
        self.assertIn("nmId", g["gap_listed"][0]["备注"])             # 在架但缺 nmId
        s = g["gap_stats"]
        self.assertEqual((s["候选vc数"], s["已推广店位"], s["候选店铺位"]), (3, 1, 5))
        self.assertEqual((s["可直接补推"], s["需先上架"], s["缺nmId"]), (3, 2, 1))
        # 快照文件缺失 → 全部归组②并标注（不误判为「可补推」）
        g2 = pg.build_gap(targets2, rows, {9352: set(), 9353: set()},
                          {9352: None, 9353: None}, min_orders=4)
        self.assertEqual(g2["gap_listed"], [])
        self.assertIn("快照", g2["gap_unlisted"][0]["备注"])
        # 阈值降到 1：CCCC（合计 1）也成候选
        g3 = pg.build_gap(targets2, rows, {9352: set(), 9353: set()}, snaps, min_orders=1)
        self.assertIn("BCS-CCCC-3", {x["vc"] for x in g3["gap_listed"] + g3["gap_unlisted"]})

        # ⑤ 反向 waste：合计 ≥ 阈值 → 不关；合计 < 阈值 → 在投活动里的推广建议关闭
        targets3 = [({}, 9352, "袁州1"), ({}, 9353, "袁州2"), ({}, 9356, "袁州3")]

        def _prod(sid, sname, cid, status, nm, vc):
            return {"shop_id": sid, "shop_name": sname, "campaign_id": cid,
                    "campaign_name": "c%d" % cid, "status_id": status, "payment_model": "cpc",
                    "budget": 100, "create_date": "", "nm": nm, "vc": vc, "cn": "名%d" % nm,
                    "src": "店快照", "ru": "R%d" % nm, "subject": "", "fbo": 0, "mp": 0}

        prods = [
            _prod(9352, "袁州1", 1, 9, 11, "BCS-AAAA-1"),     # 合计 4 → 不关
            _prod(9352, "袁州1", 1, 9, 12, "BCS-BBBB-2"),     # 合计 4 → 不关
            _prod(9352, "袁州1", 2, 11, 13, "BCS-CCCC-3"),    # 合计 1 但已暂停 → 仅计数
            _prod(9352, "袁州1", 3, 9, 14, ""),               # vc 反查不到 → 无法判定
            _prod(9356, "袁州3", 4, 9, 15, "BCS-CCCC-3"),     # 合计 1 + 在投 → 建议关闭
            _prod(9356, "袁州3", 5, 9, 15, "BCS-CCCC-3"),     # 同 nm 的第二个活动
            _prod(9352, "袁州1", 6, 4, 16, "BCS-CCCC-3"),     # 未识别状态 → 已暂停 + 告警
        ]
        w = pg.build_waste(targets3, rows, prods, min_orders=4)
        self.assertEqual([(x["shop_id"], x["campaign_id"]) for x in w["waste_close"]],
                         [(9356, 4), (9356, 5)])
        self.assertTrue(all(x["verdict"] == pg.VERDICT_CLOSE for x in w["waste_close"]))
        self.assertEqual(w["waste_close"][0]["orders"], 1)            # 三店合计单数
        self.assertEqual(w["waste_close"][0]["shop_orders"], 0)       # 袁州3 本店 0 单
        self.assertEqual(w["waste_close"][0]["shop_split"], "袁州1=1")
        self.assertIn("低于阈值", w["waste_close"][0]["备注"])
        self.assertNotIn(1, [x["campaign_id"] for x in w["waste_close"]])   # 合计达标不进清单
        self.assertEqual([x["campaign_id"] for x in w["waste_paused"]], [2, 6])
        self.assertEqual(len(w["waste_unknown"]), 1)
        self.assertEqual(w["waste_unknown_status"], [(4, 9352, 6)])
        self.assertEqual(w["waste_stats"]["被推广商品位"], 7)
        self.assertEqual(w["waste_stats"]["建议关闭"], 2)

        # ⑥ 商品级汇总：同 (店铺, nm) 跨活动合并
        bn = w["waste_by_nm"]
        self.assertEqual([(x["shop_id"], x["nm"], x["n_campaigns"], x["orders"]) for x in bn],
                         [(9356, 15, 2, 1)])

        # ⑦ 活动级汇总：建议关闭数 == 被推广商品数 → 整活动建议关闭
        bc = {(x["shop_id"], x["campaign_id"]): x for x in w["waste_by_campaign"]}
        self.assertEqual(bc[(9356, 4)]["被推广商品数"], 1)
        self.assertEqual(bc[(9356, 4)]["建议关闭数"], 1)
        self.assertTrue(bc[(9356, 4)]["是否整活动建议关闭"])
        self.assertEqual(bc[(9352, 1)]["建议关闭数"], 0)              # 合计达标 → 不关
        self.assertEqual(bc[(9352, 1)]["是否整活动建议关闭"], "")
        self.assertEqual(bc[(9352, 2)]["建议关闭数"], 0)              # 暂停 → 不算建议关闭


if __name__ == "__main__":
    unittest.main(verbosity=2)



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
        res = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if expect_code is not None:
            self.assertEqual(
                res.returncode,
                expect_code,
                f"Command failed: {' '.join(args)}\nStderr: {res.stderr}\nStdout: {res.stdout}",
            )
        return res

    # ---------------- 1. 全部 40 个命令的帮助与解析测试 ----------------
    def test_01_all_subcommand_helps(self):
        subcommands = [
            "shops", "fetch", "mapping", "mapping-import", "mapping-check",
            "mismatch-check", "review", "merge", "mapping-rename", "shops-mapping",
            "price", "stock", "trash", "replicate", "import-shelve",
            "promo-apply", "discount", "discount-wb", "discount-scan", "discount-bcs",
            "dimension", "dims-check", "banned", "clean", "price-review",
            "orders", "questions", "questions-watch", "appeals", "ai-test", "mabang-orders",
            "mabang-forecast", "feishu-register", "mabang-stock-register",
            "mabang-stock-daily", "mabang-process", "cookies-update",
            "daily", "schedule", "remote-wh"
        ]
        self.assertEqual(len(subcommands), 40)
        for subcmd in subcommands:
            with self.subTest(command=subcmd):
                res = self._run_cmd([subcmd, "--help"], expect_code=0)
                self.assertIn("usage: wb", res.stdout)

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
        res = self._run_cmd(["price", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

    def test_10_stock_dry_run(self):
        res = self._run_cmd(["stock", "--vc", "BCS-HAAJ-248364237", "--shops", "9352"], expect_code=0)
        self.assertIn("dry-run", res.stdout)

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
        try:
            res = self._run_cmd(["mapping-rename", "--vc", "BCS-TEST-TEST12345", "--cn", "单元测试商品-纠偏", "--reason", "自动化验证"], expect_code=0)
            self.assertIn("BCS-TEST-TEST12345", res.stdout)
            self.assertIn("单元测试商品-纠偏", res.stdout)
        finally:
            try:
                from wb_ops.storage.mapping_repo import MappingRepository
                ov = MappingRepository.load_vc_override()
                if "BCS-TEST-TEST12345" in ov:
                    del ov["BCS-TEST-TEST12345"]
                    MappingRepository.save_vc_override(ov)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

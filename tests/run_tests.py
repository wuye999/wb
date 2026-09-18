# -*- coding: utf-8 -*-
"""wb_ops 测试选择器：只跑「新增或修改功能」相关用例，严禁盲目全量测试。

【核心原则】
- 新增或者修改功能时，无需进行全量测试，只需要测新增或者修改的功能即可！
- 默认执行（无参数）自动探测 git 改动，仅对改动涉及的命令运行测试。
- 绝不因修改了通用/共享文件而自动触发全量测试（全量测试必须显式传入 --all 才会执行）。

用法（均在仓库根目录执行，解释器用 venv python）：
    python tests/run_tests.py                    # 默认只测改动功能（自动 git 探测，不跑全量）
    python tests/run_tests.py --cmd <命令>        # 新增/修改功能推荐：只测指定命令（如 --cmd shelve,shelve-old）
    python tests/run_tests.py --help-smoke       # 最快回归：改动模块导入检查 + 全部命令 --help 冒烟
    python tests/run_tests.py -k <关键字>         # 关键字过滤（透传 unittest -k）
    python tests/run_tests.py --list             # 打印「命令 ↔ 用例」映射
    python tests/run_tests.py --plan             # 只打印将要执行的用例，不真正执行
    python tests/run_tests.py --all              # 【注意：仅在发版等极特殊情况显式指定】全量测试（含真实平台调用，耗时约 5 分钟）
"""
import argparse
import ast
import os
import re
import subprocess
import sys
import unittest

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_FILE = os.path.join(BASE_DIR, "tests", "test_all_commands.py")
TEST_MODULE = "wb_test_all_commands"
TEST_CLASS = "TestAllCommands"
WB_SCRIPT = os.path.join(BASE_DIR, "wb.py")

# ---- 改动文件 → 受影响范围 ----
#   值为命令名列表 = 定向；["SMOKE"] = 导入检查 + 全命令 --help 冒烟；["ALL"] = 全量
#   ⚠ 顺序敏感：先精确后宽泛（首次命中即 break）
CATALOG_CMDS = ["fetch", "mapping", "mapping-import", "mapping-check", "mismatch-check",
                "review", "merge", "mapping-rename", "shops-mapping"]
DISCOUNT_CMDS = ["discount", "promo-apply", "discount-bcs", "price-review"]
ORDER_CMDS = ["orders", "mabang-orders", "mabang-forecast", "feishu-register",
              "mabang-stock-register", "mabang-stock-daily", "mabang-process"]
REPLICATE_CMDS = ["price", "stock", "trash", "replicate", "import-shelve", "dimension",
                  "dims-check", "banned", "clean", "remote-wh", "shelve", "shelve-old"]
SUPPORT_CMDS = ["questions", "questions-watch", "ai-test", "appeals"]

PATH_HINTS = [
    # 注册与声明类：只影响解析/注册/新增模型 → 冒烟即可
    ("wb.py", ["SMOKE"]),
    ("wb_ops/cli.py", ["SMOKE"]),
    ("wb_ops/framework/cli_args.py", ["SMOKE"]),
    ("wb_ops/framework/registry.py", ["SMOKE"]),
    ("wb_ops/domain/", ["SMOKE"]),
    # 业务实现（按域定向）
    ("wb_ops/services/support/complaints.py", ["appeals"]),
    ("wb_ops/adapters/callcenter_client.py", ["appeals"]),
    ("wb_ops/services/support/", SUPPORT_CMDS),
    ("wb_ops/services/replicate/dims_check.py", ["dims-check"]),
    ("wb_ops/services/replicate/dimension.py", ["dimension"]),
    ("wb_ops/services/replicate/banned.py", ["banned"]),
    ("wb_ops/services/replicate/clean.py", ["clean"]),
    ("wb_ops/services/replicate/remote_wh.py", ["remote-wh"]),
    ("wb_ops/services/replicate/shelve_new.py", ["shelve"]),
    ("wb_ops/services/replicate/shelve_old.py", ["shelve-old"]),
    ("wb_ops/services/replicate/shelve_common.py", ["shelve", "shelve-old", "replicate", "import-shelve"]),
    ("wb_ops/services/replicate/replicate.py", ["replicate"]),
    ("wb_ops/services/replicate/import_shelve.py", ["import-shelve"]),
    ("wb_ops/services/replicate/foreign_table.py", ["import-shelve"]),
    ("wb_ops/services/replicate/wb_card.py", ["replicate", "import-shelve", "shelve", "shelve-old"]),
    ("wb_ops/services/replicate/ops", ["price", "stock", "trash"]),
    ("wb_ops/services/replicate/", REPLICATE_CMDS),
    ("wb_ops/services/discount/discount_bcs.py", ["discount-bcs"]),
    ("wb_ops/services/discount/price_review.py", ["price-review"]),
    ("wb_ops/services/discount/promo.py", ["promo-apply"]),
    ("wb_ops/services/discount/", DISCOUNT_CMDS),
    ("wb_ops/services/catalog/products.py", ["fetch"]),
    ("wb_ops/services/catalog/mapping.py", ["merge", "mapping"]),
    ("wb_ops/services/catalog/mapping_sync.py", ["merge", "shops-mapping", "mapping-rename"]),
    ("wb_ops/services/catalog/mapping_excel.py", ["merge", "shops-mapping"]),
    ("wb_ops/services/catalog/mapping_check.py", ["mapping-check"]),
    ("wb_ops/services/catalog/mismatch_check.py", ["mismatch-check"]),
    ("wb_ops/services/catalog/workbench.py", ["mapping", "review"]),
    ("wb_ops/services/catalog/", CATALOG_CMDS),
    ("wb_ops/services/order/orders.py", ["orders"]),
    ("wb_ops/services/order/mabang_process.py", ["mabang-process"]),
    ("wb_ops/services/order/mabang_stock", ["mabang-stock-register", "mabang-stock-daily"]),
    ("wb_ops/services/order/mabang.py", ["mabang-orders", "mabang-forecast", "mabang-process"]),
    ("wb_ops/services/order/feishu_register.py", ["feishu-register"]),
    ("wb_ops/services/order/", ORDER_CMDS),
    # 门面（薄转发，按域定向）
    ("wb_ops/services/catalog_svc.py", CATALOG_CMDS),
    ("wb_ops/services/discount_svc.py", DISCOUNT_CMDS),
    ("wb_ops/services/order_svc.py", ORDER_CMDS),
    ("wb_ops/services/replicate_svc.py", REPLICATE_CMDS),
    ("wb_ops/services/support_svc.py", SUPPORT_CMDS),
    # 适配器
    ("wb_ops/adapters/mabang_client.py", ORDER_CMDS),
    ("wb_ops/adapters/wb_client.py", ["discount", "banned", "dims-check", "appeals", "questions", "orders"]),
    ("wb_ops/adapters/bcs_client.py", ["shops", "fetch", "stock", "price"]),
    ("wb_ops/adapters/task_runner.py", ["fetch", "orders"]),
    ("wb_ops/adapters/llm_client.py", ["questions-watch", "ai-test"]),
    ("wb_ops/adapters/cookies.py", ["cookies-update"]),
    ("wb_ops/adapters/", ["SMOKE"]),
    # 调度
    ("wb_ops/daily.py", ["daily"]),
    ("wb_ops/schedule.py", ["schedule"]),
    # 跨层共享件与通用工具：只做语法与导入冒烟，不自动升级为全量
    ("wb_ops/framework/", ["SMOKE"]),
    ("wb_ops/common.py", ["SMOKE"]),
    ("wb_ops/config.py", ["SMOKE"]),
    ("wb_ops/credentials.py", ["SMOKE"]),
    ("wb_ops/storage/", ["SMOKE"]),
    ("tests/test_all_commands.py", ["SMOKE"]),
    ("tests/run_tests.py", ["SMOKE"]),
    ("tests/", ["SMOKE"]),
]

# 纯文档/数据/临时产物改动不影响运行逻辑，不触发任何测试
IGNORE_PREFIXES = (
    "docs/", "README.md", "api/", "_scratch/", "data/", ".workbuddy/", ".trae/",
    "requirements.txt", ".gitignore", "_archive/",
)

# 名称包含匹配覆盖不到的补充映射
EXTRA = {
    "merge": ["test_28_cli_aliases"],
    "mapping-import": ["test_21_review"],
    "review": ["test_21_review"],
    # discount 的两个别名命令：以 discount 的别名用例 + 双侧区间用例为准
    "discount-wb": ["test_05_discount_aliases", "test_32_discount_aliases_below"],
    "discount-scan": ["test_05_discount_aliases", "test_32_discount_aliases_below"],
    # 以下命令的专用用例名不含命令全名，需显式登记
    "shops-mapping": ["test_18_shops_mapping"],
    "mapping-check": ["test_19_mapping_check"],
    "mismatch-check": ["test_20_mismatch_check"],
}

FULL_HINT = "（全量含真实平台调用，约 5 分钟）"


def _norm(s: str) -> str:
    """命令名/用例名归一化：去掉 - _ 与空格后小写，便于包含匹配"""
    return re.sub(r"[-_\s]", "", s).lower()


def load_test_meta():
    """ast 解析测试文件 → (命令清单, 用例方法名清单)"""
    src = open(TESTS_FILE, encoding="utf-8").read()
    tree = ast.parse(src)
    commands, methods = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "subcommands" and isinstance(node.value, ast.List):
                    commands = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            methods.append(node.name)
    return commands, sorted(methods)


def select_tests(commands, methods):
    """命令 → 用例（归一化最长匹配 + EXTRA 补充，避免 stock 误配 mabang-stock、shelve 误配 import-shelve）"""
    picked = {cmd: [] for cmd in commands}
    for m in methods:
        norm_m = _norm(m)
        matching_cmds = [c for c in commands if _norm(c) in norm_m]
        if matching_cmds:
            best_cmd = max(matching_cmds, key=lambda c: len(_norm(c)))
            picked[best_cmd].append(m)
    for cmd in commands:
        for extra in EXTRA.get(cmd, []):
            if extra in methods and extra not in picked[cmd]:
                picked[cmd].append(extra)
        picked[cmd] = sorted(set(picked[cmd]))
    return picked


def changed_paths():
    """git 探测本次改动文件（已剔除文档/数据类）"""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=BASE_DIR,
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    except Exception as e:
        print(f"[警告] git 探测失败（{e}）→ 按全量处理")
        return None
    paths, skipped = [], []
    for line in out.splitlines():
        p = line[3:].strip().strip('"')
        if " -> " in p:
            p = p.split(" -> ")[-1]
        if not p:
            continue
        p = p.replace("\\", "/")
        (skipped if p.startswith(IGNORE_PREFIXES) else paths).append(p)
    if skipped:
        print(f"[忽略] {len(skipped)} 个文档/数据类改动（不影响运行逻辑）：{', '.join(skipped[:4])}"
              + (" …" if len(skipped) > 4 else ""))
    return paths


def classify(paths):
    """改动文件 → (命令集合, 是否需冒烟, 未登记文件)
    铁律：任何改动均不自动升级为全量测试，只测相关命令或跑轻量语法/导入冒烟。
    """
    cmds, need_smoke, unknown = set(), False, []
    for p in paths:
        for frag, val in PATH_HINTS:
            if p.startswith(frag):
                if val == ["SMOKE"]:
                    need_smoke = True
                else:
                    cmds.update(val)
                break
        else:
            unknown.append(p)
            need_smoke = True
    return cmds, need_smoke, unknown


def run_import_check(paths):
    """对改动的 wb_ops 模块做导入冒烟；对改动的 tests/*.py 做语法编译检查"""
    mods, files = [], []
    for p in paths:
        if not p.endswith(".py"):
            continue
        if p.startswith("wb_ops/"):
            mod = p[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            mods.append(mod)
        elif p.startswith("tests/"):
            files.append(p)
    if not mods and not files:
        return 0
    sys.stdout.flush()
    print(f"\n[导入检查] wb_ops 模块 {len(mods)} 个 / 测试文件 {len(files)} 个 …")
    fails = []
    for m in sorted(set(mods)):
        r = subprocess.run([sys.executable, "-c", f"import {m}"], cwd=BASE_DIR,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = r.returncode == 0
        print(f"  {'✓' if ok else '✗'} {m}")
        if not ok:
            fails.append(m)
            print("      " + (r.stderr or "").strip().splitlines()[-1][:160])
    for f in sorted(set(files)):
        r = subprocess.run([sys.executable, "-m", "py_compile", f], cwd=BASE_DIR,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = r.returncode == 0
        print(f"  {'✓' if ok else '✗'} {f}（语法编译）")
        if not ok:
            fails.append(f)
            print("      " + (r.stderr or "").strip().splitlines()[-1][:160])
    if fails:
        print(f"[导入检查失败] {', '.join(fails)}")
        return 1
    return 0


def run_help_smoke(commands):
    """命令 --help 解析冒烟（快，不连平台；验证注册与参数定义无回归）"""
    print(f"\n[冒烟] {len(commands)} 个命令的 --help 解析检查 …")
    fails = []
    for cmd in commands:
        r = subprocess.run([sys.executable, WB_SCRIPT, cmd, "--help"], cwd=BASE_DIR,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = r.returncode == 0 and "usage: wb" in (r.stdout or "")
        if not ok:
            fails.append(cmd)
    print(f"  {'全部通过' if not fails else '失败: ' + ', '.join(fails)}")
    return 1 if fails else 0


def run_selected(methods):
    """按方法名加载并运行指定用例"""
    import importlib.util
    # 本脚本启动时 sys.path[0] 是 tests/ 而非仓库根；测试用例若在进程内直接
    # import wb_ops（如 test_04c 离线 mock 用例）会 ModuleNotFoundError。
    # 对齐全量入口 `python -m unittest`（cwd=仓库根）的行为，先注入仓库根。
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    spec = importlib.util.spec_from_file_location(TEST_MODULE, TESTS_FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[TEST_MODULE] = mod
    spec.loader.exec_module(mod)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for m in methods:
        suite.addTest(loader.loadTestsFromName(f"{TEST_MODULE}.{TEST_CLASS}.{m}"))
    print(f"\n[执行] {len(methods)} 个用例 …")
    sys.stdout.flush()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main():
    ap = argparse.ArgumentParser(description="wb_ops 测试选择器（只跑新增/修改功能相关用例，无需全量测试）")
    ap.add_argument("--cmd", default="", help="指定命令名（逗号分隔）：只跑该命令用例 + 这些命令的 --help 冒烟")
    ap.add_argument("--changed", action="store_true", help="用 git 探测本次改动 → 自动只跑改动相关命令（默认行为）")
    ap.add_argument("--help-smoke", action="store_true", help="改动模块导入检查 + 全部命令 --help 冒烟（最快回归）")
    ap.add_argument("-k", "--keyword", default="", help="关键字过滤（透传 unittest -k）")
    ap.add_argument("--list", action="store_true", help="打印「命令 ↔ 用例」映射表")
    ap.add_argument("--plan", action="store_true", help="只打印将要执行的用例，不执行")
    ap.add_argument("--all", action="store_true", help="【显式手动】全量测试（极耗时且真连平台，开发/修改功能时无需使用）")
    args = ap.parse_args()

    commands, methods = load_test_meta()
    mapping = select_tests(commands, methods)

    # 1. 显式全量模式（只有传了 --all 才会执行）
    if args.all:
        if args.plan:
            print(f"[计划] 显式指定全量：{len(methods)} 个用例 {FULL_HINT}")
            return 0
        return run_selected(methods)

    if args.list:
        print(f"命令 {len(commands)} 个 / 用例 {len(methods)} 个\n")
        for cmd in commands:
            hits = mapping.get(cmd) or []
            print(f"  {cmd:24s} → {', '.join(hits) if hits else '（无专属用例，仅 --help 冒烟）'}")
        return 0

    if args.keyword and not (args.cmd or args.changed or args.help_smoke):
        return run_keyword(args.keyword)

    # 2. 默认模式：未显式指定过滤时，默认只跑改动相关（--changed），绝不自动全量
    if not (args.cmd or args.changed or args.help_smoke):
        args.changed = True

    # ---------- 收集目标 ----------
    picked, paths, need_smoke = set(), [], bool(args.help_smoke)
    if args.changed:
        paths = changed_paths()
        if paths is None:
            print("[提示] git 状态无法获取，默认执行导入检查与命令冒烟")
            need_smoke = True
        else:
            p_cmds, need_smoke_flag, unknown = classify(paths)
            picked |= p_cmds
            need_smoke = need_smoke or need_smoke_flag
            if unknown:
                print(f"[提示] 未登记映射的文件改动（执行语法与导入冒烟）：{', '.join(unknown)}")
            if not paths:
                print("\n[结果] 当前工作区无相关代码改动，无需执行测试（跳过）")
                return 0
    if args.cmd:
        picked |= {c.strip() for c in args.cmd.split(",") if c.strip()}

    unknown_cmds = sorted(c for c in picked if c not in commands)
    if unknown_cmds:
        print(f"[警告] 未知命令（不在测试清单内，仅跑其 --help）: {', '.join(unknown_cmds)}")

    if not picked and not need_smoke:
        print("[提示] 无改动命令或未指定命令，无需跑测试")
        return 0

    # ---------- 冒烟 + 定向执行（只跑新增/修改功能） ----------
    targets = sorted({m for c in picked for m in mapping.get(c, [])})
    print(f"[选中命令] {', '.join(sorted(picked)) or '（仅改动模块冒烟）'}")
    for c in sorted(picked):
        hits = mapping.get(c) or []
        print(f"  {c:24s} → {', '.join(hits) if hits else '（无专属用例 → 仅 --help 冒烟）'}")
    smoke_desc = f"改动模块导入检查 + 全部 {len(commands)} 个命令" if need_smoke else "选中命令的 --help 冒烟"
    print(f"[冒烟范围] {smoke_desc}")
    if not picked:
        # 仅底层通用文件改动：全命令冒烟即可，不跑任何重型用例
        targets = []
        picked = set(commands)
    smoke_scope = list(commands) if need_smoke else sorted(picked)
    if args.plan:
        print(f"\n[计划] 用例 {len(targets)} 个 + --help 冒烟 {len(smoke_scope)} 个命令")
        return 0

    code = run_import_check(paths) if need_smoke else 0
    if targets:
        sys.stdout.flush()
        code = run_selected(targets) or code
    sys.stdout.flush()
    code = run_help_smoke(smoke_scope) or code
    print("\n[结果] " + ("全部通过" if code == 0 else "存在失败，见上方输出"))
    return code


def run_keyword(keyword: str) -> int:
    """按关键字跑用例（等价 python -m unittest -k）"""
    print(f"\n[执行] 关键字过滤 -k {keyword} …")
    r = subprocess.run([sys.executable, "-m", "unittest", "tests/test_all_commands.py", "-k", keyword],
                       cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((r.stdout or "")[-2000:])
    if r.returncode != 0:
        print((r.stderr or "")[-1500:])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())

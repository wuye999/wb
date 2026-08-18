# -*- coding: utf-8 -*-
"""
wb_ops 每日任务启动器（原 wb_daily_task.py）

把「参加促销 + 改折扣」串成每日自动执行的命令，供 Windows 计划任务调用。
morning = 报名 + 改价；check = 只改价。
"""
import datetime
import os
import subprocess
import sys

from . import config
BASE = config.REPO_ROOT
LOG_DIR = config.LOG_DIR
WB_PY = os.path.join(config.REPO_ROOT, "wb.py")


def _python_exe():
    """当前解释器对应的 python.exe（若运行在 pythonw.exe 下则切换回 python.exe）"""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        return exe[:-len("pythonw.exe")] + "python.exe"
    return exe


def _pythonw_exe():
    """当前解释器对应的 pythonw.exe（无窗口，供计划任务入口）"""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        return exe
    return exe[:-len("python.exe")] + "pythonw.exe"


PY = _python_exe()
PYW = _pythonw_exe()

# 每日步骤定义：morning = 报名 + 改价；check = 只改价
STEPS = {
    "morning": [("参加促销", [PY, WB_PY, "promo-apply", "--apply"]),
                ("改折扣", [PY, WB_PY, "discount", "--apply"])],
    "check": [("改折扣", [PY, WB_PY, "discount", "--apply"])],
}


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = os.path.join(LOG_DIR, "daily_" + datetime.date.today().strftime("%Y%m%d") + ".log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def run_step(name, argv):
    cmd = argv
    log(f"--- [{name}] {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for ln in (r.stdout or "").splitlines()[-10:]:
            log(f"    out> {ln}")
        for ln in (r.stderr or "").splitlines()[-5:]:
            log(f"    err> {ln}")
        log(f"--- [{name}] 退出码={r.returncode}")
        blob = (r.stdout or "") + (r.stderr or "")
        if "403" in blob or "cookie" in blob.lower():
            log(">>> 检测到 403/cookie 失效：请刷新 credentials.json（WB cookie）或 bcs.token（BCS 401）")
        return r.returncode
    except Exception as e:
        log(f"--- [{name}] 异常 {e}")
        return 1


def run(mode, extra=None):
    """执行 daily 任务。mode ∈ {morning, check}；extra 为透传参数列表（如 --shops 5272）"""
    extra = extra or []
    log(f"===== {mode} 开始 =====")
    codes = [run_step(name, argv + extra) for name, argv in STEPS[mode]]
    log(f"===== {mode} 结束 成功={codes.count(0)}/{len(codes)} =====")
    return 0 if all(c == 0 for c in codes) else 1

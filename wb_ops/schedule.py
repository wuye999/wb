# -*- coding: utf-8 -*-
"""
wb_ops Windows 计划任务管理（原 create_schedule.py）

用 subprocess 调 schtasks 创建/删除每日自动任务（规避中文路径命令行乱码）。
创建前会先删除旧版同名任务，指向新的统一入口 wb.py。
"""
import os
import subprocess
import sys

from . import config
from . import daily
BASE = config.REPO_ROOT
PYW = daily.PYW
SCRIPT = os.path.join(BASE, "wb.py")

# 任务定义表（集中维护）：(任务名, 每日时间, wb.py 子命令参数)
TASKS = [
    ("WB_Daily_Morning", "09:00", "daily morning"),
    ("WB_Daily_Check_11", "11:00", "daily check"),
    ("WB_Daily_Check_15", "15:00", "daily check"),
    ("WB_Daily_Check_19", "19:00", "daily check"),
]


def sh(args):
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stdout.strip():
        print("  " + r.stdout.strip())
    if r.stderr.strip():
        print("  " + r.stderr.strip(), file=sys.stderr)
    return r.returncode


def create_all():
    print("=== 创建计划任务 ===")
    for name, st, param in TASKS:
        tr = f'"{PYW}" "{SCRIPT}" {param}'  # 整串传给 schtasks，运行时按 CommandLineToArgvW 解析
        code = sh(["schtasks", "/Create", "/F", "/TN", name,
                   "/TR", tr, "/SC", "DAILY", "/ST", st])
        print(f"  {name} {st} {param} -> {'OK' if code == 0 else 'FAIL'}")
    print("\n=== 验证任务存在性 ===")
    for name, _, _ in TASKS:
        sh(["schtasks", "/Query", "/TN", name])


def remove_all():
    print("=== 删除计划任务 ===")
    for name, _, _ in TASKS:
        code = sh(["schtasks", "/Delete", "/F", "/TN", name])
        print(f"  {name} -> {'OK' if code == 0 else 'FAIL'}")


def run(args):
    (remove_all if args.remove else create_all)()
    return 0

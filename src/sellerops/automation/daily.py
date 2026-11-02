"""Daily workflows, previewed unless explicitly applied."""
import datetime
import os
from pathlib import Path
import subprocess
import sys

from sellerops.settings import paths as config

WORKFLOWS = {'morning': ('promo-apply', 'discount'), 'check': ('discount',)}


def build_steps(mode: str, apply: bool = False, shops: str = '') -> list[list[str]]:
    steps = []
    for command in WORKFLOWS[mode]:
        argv = [sys.executable, '-m', 'sellerops', '--data-dir', config.DATA_DIR, command]
        if apply:
            argv.append('--apply')
        if shops:
            argv += ['--shops', shops]
        steps.append(argv)
    return steps


def run_daily_cmd(args) -> int:
    steps = build_steps(args.mode, args.apply, args.shops)
    if not args.apply:
        for step in steps:
            print('[dry-run]', subprocess.list2cmdline(step))
        return 0
    log_dir = Path(config.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f'daily_{datetime.date.today():%Y%m%d}.log'
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[2])
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (package_root, env.get('PYTHONPATH'))))
    env['PYTHONUTF8'] = '1'
    for step in steps:
        result = subprocess.run(step, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', env=env, timeout=3600,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with log_path.open('a', encoding='utf-8') as log:
            log.write(f'[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {step[-1]} exit={result.returncode}\n')
            log.write((result.stdout or '') + (result.stderr or ''))
        if result.returncode:
            print(f'[错误] 步骤失败，后续任务已停止。日志: {log_path}')
            return result.returncode
    print(f'任务完成。日志: {log_path}')
    return 0

"""Explicit BCS session maintenance command."""
from argparse import Namespace
from pathlib import Path

from sellerops.integrations.bcs.auth import refresh_login
from sellerops.settings import paths


def run(args: Namespace) -> int:
    har = Path(args.from_har).expanduser() if args.from_har else None
    if har is not None and not har.is_absolute():
        har = Path(paths.HAR_DIR) / har
    refresh_login(har=har)
    print('BCS 账号已核对，本地会话已更新。')
    return 0

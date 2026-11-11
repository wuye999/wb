"""Explicit authentication command; no order or forecast mutations."""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sellerops.integrations.mabang.auth import refresh_login
from sellerops.settings import credentials, paths


def run(args: Namespace) -> int:
    har = Path(args.from_har).expanduser() if args.from_har else None
    if har is not None and not har.is_absolute():
        har = Path(paths.HAR_DIR) / har
    cred = credentials.reload().data.get('mabang') or {}
    refresh_login(cred, har=har)
    stamp = datetime.fromisoformat(cred['auth_updated_at']).astimezone(timezone(timedelta(hours=8)))
    mode = 'HAR 导入' if har is not None else '密码登录'
    print(f'马帮{mode}成功：www、aamz 和 API 凭证已更新（{stamp:%Y-%m-%d %H:%M:%S} UTC+8）。')
    return 0

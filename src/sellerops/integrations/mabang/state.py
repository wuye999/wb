"""Merge only authentication state while preserving concurrent business configuration."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import requests

from sellerops.core.files import FileLock, atomic_dump_json, safe_load_json
from sellerops.settings import credentials, paths
from sellerops.integrations.mabang.session import (
    AAMZ_BASE, AUTH_FIELDS, WWW_BASE, MabangAuthError, auth_signature, cookie_header, dump_cookies,
)


def auth_lock() -> FileLock:
    return FileLock(Path(paths.STATE_DIR) / 'mabang_auth', timeout=240)


def copy_auth(target: dict, source: dict) -> None:
    for key in AUTH_FIELDS:
        if key in source:
            target[key] = deepcopy(source[key])
        else:
            target.pop(key, None)


def reload_auth(target: dict) -> None:
    data = safe_load_json(paths.CREDENTIALS_JSON, default={}).get('mabang') or {}
    old_user = (target.get('login') or {}).get('username')
    new_user = (data.get('login') or {}).get('username')
    if old_user and new_user and old_user != new_user:
        raise MabangAuthError('马帮登录账号在运行期间发生变化，已停止当前任务。')
    if data:
        copy_auth(target, data)


def snapshot(candidate: dict, session: requests.Session) -> dict:
    result = deepcopy(candidate)
    result['cookie_jar'] = dump_cookies(session)
    result['www_cookie'] = cookie_header(session, WWW_BASE)
    result['aamz_cookie'] = cookie_header(session, AAMZ_BASE)
    result['auth_updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return result


def commit_auth(target: dict, candidate: dict, *, expected: str | None = None) -> bool:
    with FileLock(paths.CREDENTIALS_JSON):
        data = safe_load_json(paths.CREDENTIALS_JSON, default={})
        current = data.setdefault('mabang', {})
        old_user = (current.get('login') or {}).get('username')
        new_user = (candidate.get('login') or {}).get('username')
        if old_user and new_user and old_user != new_user:
            raise MabangAuthError('HAR 账号与现有马帮登录账号不一致，未覆盖凭证。')
        if expected is not None and auth_signature(current) != expected:
            copy_auth(target, current)
            return False
        copy_auth(current, candidate)
        atomic_dump_json(paths.CREDENTIALS_JSON, data, use_lock=False)
        copy_auth(target, current)
    credentials.reload()
    return True

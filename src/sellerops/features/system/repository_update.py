"""Best-effort WorkBuddy update notices, throttled independently of platform checks."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile
from time import time

from sellerops.core.files import FileLock
from sellerops.integrations import github_compare
from sellerops.settings import paths


_REMOTE = 'https://github.com/wuye999/wb.git'
_MAIN_REF = 'refs/heads/main'
_SUCCESS_TTL = 24 * 3600
_FAILURE_TTL = 3600


def _main_head(root: Path) -> str | None:
    git_dir = root / '.git'
    if git_dir.is_file():
        pointer = git_dir.read_text(encoding='utf-8').strip()
        if not pointer.startswith('gitdir: '):
            return None
        git_dir = root / pointer.removeprefix('gitdir: ')
    if not git_dir.is_dir():
        return None
    if (git_dir / 'HEAD').read_text(encoding='ascii').strip() != f'ref: {_MAIN_REF}':
        return None
    common = git_dir
    if (git_dir / 'commondir').is_file():
        common = git_dir / (git_dir / 'commondir').read_text(encoding='utf-8').strip()
    try:
        head = (common / _MAIN_REF).read_text(encoding='ascii').strip()
    except FileNotFoundError:
        head = ''
        with (common / 'packed-refs').open(encoding='utf-8') as refs:
            for line in refs:
                value, _, ref = line.strip().partition(' ')
                if ref == _MAIN_REF:
                    head = value
                    break
    return head if re.fullmatch(r'[0-9a-f]{40}', head) else None


def _result(status: str, *, source=None, checked_at=None, ahead_by=None) -> dict:
    timestamp = datetime.fromtimestamp(checked_at, timezone.utc).isoformat(timespec='seconds') if checked_at is not None else None
    return {'status': status, 'source': source, 'checked_at': timestamp, 'ahead_by': ahead_by}


def _valid_comparison(result: dict) -> bool:
    count = result.get('ahead_by')
    status = result.get('status')
    if type(count) is not int:
        return False
    if status in ('update_available', 'diverged'):
        return count > 0
    return status in ('up_to_date', 'local_ahead') and count == 0


def _cached(path: Path, identity: dict) -> dict | None:
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict) or any(record.get(key) != value for key, value in identity.items()):
            return None
        checked_at = record.get('checked_at')
        if type(checked_at) not in (int, float):
            return None
        status, ahead_by = record.get('status'), record.get('ahead_by')
        if status == 'unavailable' and ahead_by is None:
            ttl = _FAILURE_TTL
        elif _valid_comparison(record):
            ttl = _SUCCESS_TTL
        else:
            return None
        if 0 <= time() - checked_at < ttl:
            return _result(status, source='cache', checked_at=checked_at, ahead_by=ahead_by)
    except (OSError, ValueError, OverflowError, RecursionError):
        pass
    return None


def _probe(head: str, *, runner=None) -> dict:
    if runner is None:
        runner = subprocess.run
    completed = runner([sys.executable, '-I', github_compare.__file__, head],
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       encoding='utf-8', check=True, timeout=2, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or not _valid_comparison(result):
        raise ValueError('Invalid GitHub probe result')
    return {key: result[key] for key in ('status', 'ahead_by')}


def _write_cache(path: Path, record: dict) -> None:
    temp = None
    try:
        with NamedTemporaryFile(mode='w', prefix=f'.{path.name}.', dir=path.parent,
                                encoding='utf-8', delete=False) as stream:
            temp = Path(stream.name)
            json.dump(record, stream, ensure_ascii=False)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def check(*, force: bool = False) -> dict:
    try:
        root = paths.PROJECT_ROOT.resolve()
        head = _main_head(root) if shutil.which('git') else None
        if head is None:
            return _result('not_checked')
        cache = Path(paths.current.repository_update_json)
        identity = {'version': 1, 'project_root': str(root), 'remote': _REMOTE, 'branch': 'main', 'head': head}
        if not force and (result := _cached(cache, identity)) is not None:
            return result
        with FileLock(cache, timeout=0):
            if not force and (result := _cached(cache, identity)) is not None:
                return result
            try:
                comparison = _probe(head)
            except Exception:
                comparison = {'status': 'unavailable', 'ahead_by': None}
            if _main_head(root) != head:
                return _result('not_checked')
            checked_at = time()
            _write_cache(cache, {**identity, **comparison, 'checked_at': checked_at})
            return _result(**comparison, source='live', checked_at=checked_at)
    except Exception:
        return _result('unavailable')


def print_reminder(result: dict) -> None:
    if result['status'] == 'update_available':
        print(f"[更新提醒] GitHub 主分支比本机多 {result['ahead_by']} 个提交。"
              f"需要时可在项目目录运行 git pull --ff-only {_REMOTE} main。")

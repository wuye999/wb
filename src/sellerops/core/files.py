"""Atomic state writes and process locks; corrupt state is never silently reset."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from sellerops.core.errors import DataConsistencyError, StorageLockError

_guard = threading.Lock()
_locks: dict[str, Any] = {}
_local = threading.local()


class FileLock:
    """OS process lock with thread exclusion and reentrancy.

    The .lock file persists. Ownership is released by the OS on process exit.
    An active lock is never stolen based on elapsed time.
    """

    def __init__(self, target_filepath: str | Path, timeout: float = 10.0) -> None:
        self.target = os.path.normcase(os.path.abspath(target_filepath))
        self.timeout = timeout
        self._depth = 0
        with _guard:
            self._thread_lock = _locks.setdefault(self.target, threading.RLock())

    def acquire(self) -> FileLock:
        start = time.monotonic()
        if not self._thread_lock.acquire(timeout=max(0, self.timeout)):
            raise StorageLockError(self.target, self.timeout)
        held = getattr(_local, 'held', None)
        if held is None:
            held = _local.held = {}
        if self.target in held:
            held[self.target][0] += 1
            self._depth += 1
            return self
        handle = None
        try:
            lock_path = Path(self.target + '.lock')
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open('a+b')
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'\0')
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held[self.target] = [1, handle]
                    self._depth += 1
                    return self
                except OSError:
                    if time.monotonic() - start >= self.timeout:
                        raise StorageLockError(self.target, self.timeout)
                    time.sleep(0.02)
        except BaseException:
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if not self._depth:
            return
        held = _local.held
        held[self.target][0] -= 1
        if held[self.target][0] == 0:
            _, handle = held.pop(self.target)
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self._depth -= 1
        self._thread_lock.release()

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


def atomic_write_text(filepath: str | Path, text: str, *, use_lock: bool = True,
                      timeout: float = 10.0) -> None:
    target = Path(filepath).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(target, timeout) if use_lock else nullcontext():
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.' + target.name + '.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(10):
                try:
                    os.replace(tmp, target)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.03 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def atomic_dump_json(filepath: str | Path, data: Any, indent: int = 2,
                     use_lock: bool = True, timeout: float = 10.0) -> None:
    atomic_write_text(filepath, json.dumps(data, ensure_ascii=False, indent=indent) + '\n',
                      use_lock=use_lock, timeout=timeout)


def safe_load_json(filepath: str | Path, default: Any = None,
                   use_lock: bool = False, timeout: float = 5.0) -> Any:
    with FileLock(filepath, timeout) if use_lock else nullcontext():
        try:
            with open(filepath, encoding='utf-8-sig') as stream:
                return json.load(stream)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DataConsistencyError(f'JSON 文件损坏: {filepath}') from exc

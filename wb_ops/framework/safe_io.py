# -*- coding: utf-8 -*-
"""
wb_ops 统一安全文件持久化工具 (SafeFileStorage)

功能特性：
1. 原子文件写入 (Atomic Write)：通过写入临时文件后执行 os.replace 覆盖，内置针对 Windows 文件锁定的指数退避重试。
2. 跨进程原子互斥锁 (FileLock)：基于原子系统调用实现，零三方库依赖，支持超时等待与死锁自愈。
3. 统一容错读取 (safe_load_json)：支持默认值兜底与短暂重试。
"""
import os
import sys
import json
import time
import tempfile
import threading
import collections
from .exceptions import StorageLockError

_local_locks = threading.local()


def _get_held_locks():
    if not hasattr(_local_locks, "held"):
        _local_locks.held = collections.defaultdict(int)
    return _local_locks.held


class FileLock:
    """基于文件系统原子 mkdir 的跨进程排他锁 (纯标准库，支持可重入与死锁自愈)"""
    def __init__(self, target_filepath: str, timeout: float = 10.0, stale_after: float = 300.0):
        self.target = os.path.abspath(target_filepath)
        self.lock_dir = os.path.normcase(os.path.abspath(self.target + ".__lock__"))
        self.timeout = timeout
        self.stale_after = stale_after
        self.acquired = False

    def acquire(self):
        held = _get_held_locks()
        if held[self.lock_dir] > 0:
            held[self.lock_dir] += 1
            self.acquired = True
            return self

        start_time = time.time()
        while True:
            try:
                os.mkdir(self.lock_dir)
                held[self.lock_dir] = 1
                self.acquired = True
                try:
                    meta_path = os.path.join(self.lock_dir, "meta.txt")
                    with open(meta_path, "w", encoding="utf-8") as f:
                        f.write(f"{time.time()}|{os.getpid()}|{threading.get_ident()}\n")
                except Exception:
                    pass
                return self
            except FileExistsError:
                try:
                    meta_path = os.path.join(self.lock_dir, "meta.txt")
                    mtime = os.path.getmtime(meta_path) if os.path.exists(meta_path) else (
                        os.path.getmtime(self.lock_dir) if os.path.exists(self.lock_dir) else time.time()
                    )
                    if time.time() - mtime > self.stale_after:
                        if os.path.exists(meta_path):
                            try:
                                os.remove(meta_path)
                            except Exception:
                                pass
                        try:
                            os.rmdir(self.lock_dir)
                        except Exception:
                            pass
                        continue
                except Exception:
                    pass

                if time.time() - start_time >= self.timeout:
                    raise StorageLockError(self.target, self.timeout)
                time.sleep(0.05)

    def release(self):
        if self.acquired:
            held = _get_held_locks()
            if held[self.lock_dir] > 0:
                held[self.lock_dir] -= 1
                if held[self.lock_dir] == 0:
                    del held[self.lock_dir]
                    try:
                        meta_path = os.path.join(self.lock_dir, "meta.txt")
                        if os.path.exists(meta_path):
                            try:
                                os.remove(meta_path)
                            except Exception:
                                pass
                        for _ in range(5):
                            try:
                                if os.path.exists(self.lock_dir):
                                    os.rmdir(self.lock_dir)
                                break
                            except OSError:
                                time.sleep(0.02)
                    except Exception:
                        pass
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def atomic_dump_json(filepath: str, data, indent: int = 2, use_lock: bool = True, timeout: float = 10.0):
    """先写入同目录隐藏临时文件，fsync 刷盘后原子 rename 覆盖目标文件。"""
    target_abs = os.path.abspath(filepath)
    dir_name = os.path.dirname(target_abs)
    os.makedirs(dir_name, exist_ok=True)

    def _do_write():
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_atomic_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            
            # Windows 下若目标文件被短暂占用，os.replace 会报 PermissionError，重试即可
            retries = 15
            for attempt in range(retries):
                try:
                    os.replace(tmp_path, target_abs)
                    break
                except (PermissionError, OSError):
                    if attempt == retries - 1:
                        raise
                    time.sleep(0.03 * (attempt + 1))
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise

    if use_lock:
        with FileLock(target_abs, timeout=timeout):
            _do_write()
    else:
        _do_write()


def safe_load_json(filepath: str, default=None, use_lock: bool = False, timeout: float = 5.0):
    """安全读取 JSON 文件，文件不存在时返回 default，损坏时返回 default 并重试。"""
    target_abs = os.path.abspath(filepath)
    if not os.path.exists(target_abs):
        return default

    def _do_read():
        retries = 10
        for attempt in range(retries):
            try:
                with open(target_abs, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (PermissionError, OSError):
                if attempt == retries - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))

    try:
        if use_lock:
            with FileLock(target_abs, timeout=timeout):
                return _do_read()
        else:
            return _do_read()
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return default

"""Cooperative request deadlines for BCS snapshot jobs."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
import time
from typing import Iterator

from sellerops.core.errors import NetworkError, StorageLockError
from sellerops.core.files import FileLock


class SyncInterrupted(NetworkError):
    def __init__(self, kind: str, reason: str) -> None:
        self.kind = kind
        super().__init__(reason)


@dataclass(frozen=True)
class RequestBudget:
    deadline: float
    stop: Event | None = None
    timeout_kind: str = 'batch_timeout'

    def remaining(self) -> float:
        if self.stop is not None and self.stop.is_set():
            raise SyncInterrupted('authentication_aborted', 'BCS 鉴权熔断，已停止后续请求')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            reason = ('等待 BCS 同步进度超时' if self.timeout_kind == 'shop_timeout'
                      else 'BCS 远程阶段已达时间预算')
            raise SyncInterrupted(self.timeout_kind, reason)
        return remaining

    def timeout(self, limits: float | tuple[float, float]) -> tuple[float, float]:
        connect, read = limits if isinstance(limits, tuple) else (limits, limits)
        half = self.remaining() / 2
        return min(connect, half), min(read, half)

    def pause(self, seconds: float) -> None:
        delay = min(seconds, self.remaining())
        if self.stop is None:
            time.sleep(delay)
        else:
            self.stop.wait(delay)
        self.remaining()


@contextmanager
def refresh_lock(path: Path, timeout: float, budget: RequestBudget | None) -> Iterator[None]:
    if budget is None:
        with FileLock(path, timeout=timeout):
            yield
        return
    lock_deadline = time.monotonic() + timeout
    while True:
        remaining = budget.remaining()
        left = lock_deadline - time.monotonic()
        if left <= 0:
            raise StorageLockError(path, timeout)
        # Recheck the shared stop signal while waiting on another process/thread.
        lock = FileLock(path, timeout=min(0.25, remaining, left))
        try:
            lock.acquire()
        except StorageLockError:
            continue
        try:
            budget.remaining()
            yield
        finally:
            lock.release()
        return

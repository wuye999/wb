# -*- coding: utf-8 -*-
"""
wb_ops 通用异步任务引擎 (AsyncTaskRunner)
统一封装平台异步任务提交、退避轮询与超时监控。
"""
import time
from typing import Callable, Any, Optional
from ..framework.exceptions import TaskTimeoutError, PlatformApiError


class AsyncTaskRunner:
    """通用异步任务引擎"""

    @staticmethod
    def run_until_complete(
        submit_fn: Callable[[], Any],
        check_fn: Optional[Callable[[Any], bool]] = None,
        timeout: int = 180,
        interval: float = 3.0,
        max_retries: int = 3,
    ) -> Any:
        """提交任务并可选轮询至结束。
        
        若 check_fn 为 None，则仅执行 submit_fn 并返回其结果（适用于无需轮询的立即提交场景）。
        """
        last_err = None
        for attempt in range(max_retries):
            try:
                task_id = submit_fn()
                if not check_fn or not task_id:
                    return task_id

                start_time = time.time()
                while time.time() - start_time < timeout:
                    is_done = check_fn(task_id)
                    if is_done:
                        return task_id
                    time.sleep(interval)

                raise TaskTimeoutError(task_id, timeout)
            except Exception as e:
                last_err = e
                if attempt == max_retries - 1:
                    raise
                time.sleep(interval * (attempt + 1))

        if last_err:
            raise last_err

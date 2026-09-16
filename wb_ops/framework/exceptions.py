# -*- coding: utf-8 -*-
"""
wb_ops 统一异常层次体系
"""


class WBOpsError(Exception):
    """wb_ops 基础异常"""
    pass


class AuthenticationError(WBOpsError):
    """凭证缺失、鉴权失败或 Cookie/Token 过期 (HTTP 401/403)"""
    def __init__(self, message="凭证无效或已过期，请刷新 credentials.json", shop_id=None):
        self.shop_id = shop_id
        super().__init__(f"[Shop {shop_id}] {message}" if shop_id else message)


class RateLimitError(WBOpsError):
    """平台接口调用超频被限流 (HTTP 429)"""
    def __init__(self, message="请求过于频繁，触发平台限频保护", retry_after=5):
        self.retry_after = retry_after
        super().__init__(f"{message} (建议等待 {retry_after}s)")


class PlatformApiError(WBOpsError):
    """平台服务端异常或业务返回错误码"""
    def __init__(self, message, status_code=None, raw_response=None):
        self.status_code = status_code
        self.raw_response = raw_response
        super().__init__(f"平台接口异常 [{status_code}]: {message}" if status_code else f"平台接口异常: {message}")


class TaskTimeoutError(WBOpsError):
    """异步任务轮询超时"""
    def __init__(self, task_id, timeout_sec):
        self.task_id = task_id
        self.timeout_sec = timeout_sec
        super().__init__(f"任务 {task_id} 执行超时 (已等待 {timeout_sec} 秒未完成)")


class StorageLockError(WBOpsError):
    """文件排他锁获取超时（并发竞争）"""
    def __init__(self, filepath, timeout_sec):
        self.filepath = filepath
        self.timeout_sec = timeout_sec
        super().__init__(f"获取文件锁超时 ({timeout_sec}s): {filepath} 正在被其他进程操作")


class ValidationError(WBOpsError):
    """参数或数据格式校验不合规"""
    pass

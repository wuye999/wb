# -*- coding: utf-8 -*-
"""
wb_ops 通用 HTTP / 工具层

放共享的小工具与异常：UA 常量、CookieExpiredError、jwt_payload、to_int、stdout UTF-8。
BCS 与 WB 两套请求的重试策略各不相同，分别封装在 bcs.py / wb_api.py 中。
"""
import base64
import json
import sys


class CookieExpiredError(RuntimeError):
    """WB cookie 失效（HTTP 403，cfidsw-wb 过期）"""


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def jwt_payload(jwt):
    """解码 JWT 的 payload 段 → dict（用于取 Z-Sid 等）"""
    seg = jwt.split(".")[1]
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def to_int(v, default=0):
    """discount 等字段可能是 int/str/None，统一转 int"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def ensure_utf8_stdout():
    """Windows 控制台强制 UTF-8 输出（避免中文乱码）。库环境下无 reconfigure 时静默跳过。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def extract_wb_nm(vc):
    """从 vendorCode 提取 WB原始nmId（纯数字才有效，否则返回 None）。
    兼容：
    1. 经典/他人表格式：BCS-{前缀}-{WB原始nmId} 或 BCS-{前缀}-ozon-card-{WB原始nmId}
    2. 新供应商代码格式：BCS-{前缀}-{标识}/{WB原始nmId}（如 BCS-QQNN-WRLINWI/1078999444）
    """
    s = str(vc or "").strip()
    if "/" in s:
        tail = s.rsplit("/", 1)[-1]
    else:
        tail = s.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else None


def print_write_hint():
    """写操作未加 --sync 时打印的提示：告知用户当前未做写后验证/同步/合并，以及如何补做。"""
    print("\n[提示] 本次未做写后验证。因 WB/BCS 存在异步回填与延迟，当场验证不一定准确。")
    print("       如需同步在架商品并合并到映射表，请运行：python wb.py fetch && python wb.py merge")
    print("       （或在原写命令后加 --sync，命令内自动完成同步+合并）")



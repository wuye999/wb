# -*- coding: utf-8 -*-
"""
wb_ops 通用 HTTP / 工具层

放共享的小工具与异常：UA 常量、CookieExpiredError、jwt_payload、to_int、stdout UTF-8。
BCS 与 WB 两套请求的重试策略各不相同，分别封装在 bcs.py / wb_api.py 中。
"""
import base64
import json
import os
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
    """Windows 控制台强制 UTF-8 输出（避免中文乱码）。库环境下无 reconfigure 时静默跳过。

    ⚠ 同时**开启行缓冲**：`python wb.py xxx > log.txt` 时 stdout 默认是 8KB 全缓冲，
    长任务（如马帮上传轮询最长 15 分钟）期间日志迟迟不落盘，外部看起来就像「卡住」。
    行缓冲后每行即 flush，重定向文件/管道也能实时观察进度。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def write_csv_preferred(log_dir, name_cn, name_alias, ts, writer):
    """写 CSV 报告：优先用中文名（模板含 `{ts}`），失败时回退 ASCII 别名。

    ⚠ 背景（2026-10-17）：部分运行环境会按「文件名特征」拦截含第三方系统关键词
    （实测 `马帮` / `飞书` / `库存SKU`）的写入，抛 PermissionError。报告只是副产物，
    不应因此中断订单主链路 ⇒ 回退到 ASCII 别名并提示；两者都失败也只告警、返回 ""。

    :param writer: 回调 `writer(file_obj)`，负责写表头与数据行
    :return: 实际写入的完整路径；两种命名都失败时返回 ""
    """
    os.makedirs(log_dir, exist_ok=True)
    errs = []
    for idx, tpl in enumerate((name_cn, name_alias)):
        path = os.path.join(log_dir, tpl.format(ts=ts))
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer(f)
        except OSError as e:          # PermissionError / 目录只读 / 文件被占用
            errs.append((path, e))
            continue
        if idx:
            print(f"[提示] 报告已用备用文件名写入: {os.path.basename(path)}")
        return path
    path, e = errs[-1]
    print(f"[警告] 报告未生成（不影响主流程）: {os.path.basename(path)}"
          f" → {type(e).__name__}: {e}")
    return ""


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



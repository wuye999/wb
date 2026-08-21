# -*- coding: utf-8 -*-
"""
wb_ops AI 客服离线对照测试

读 data/ai_test_qa.json（真人问答对照数据集），对每条真实买家提问调用 ai_reply
生成 AI 回复，终端打印并把「商品 | 买家问题 | 人工客服回复 | AI 回复」写成
Markdown 报告（data/logs/ai_reply_test_*.md），供肉眼审查、迭代调 prompt。

纯离线：不访问 WB，只用数据集自带 product 字段拼商品信息，无需 cookie。
"""
import json
import os
import time

from . import ai_reply
from . import common
from . import config
from . import credentials

# product 字段 → 展示标签 的有序映射（空值跳过）
_FIELDS = [("cn", "中文名"), ("title", "标题"), ("brand", "品牌"),
           ("colors", "颜色"), ("price", "价格"),
           ("description", "商品描述"), ("options", "商品特征")]


def build_product_info(p):
    """把 product 字段拼成多行商品信息串（与 questions_watch.product_info_str 同风格）。"""
    if not p:
        return "(无商品信息)"
    parts = []
    for key, label in _FIELDS:
        v = (p.get(key) or "").strip()
        if v:
            parts.append(f"{label}：{v}")
    return "\n".join(parts) if parts else "(无商品信息)"


def _md(t):
    """Markdown 单元格转义：表格符转义、换行转 <br>。"""
    return t.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    cfg = {
        "base_url": cred.ai_base_url,
        "model": cred.ai_model,
        "api_key": cred.ai_key,
        "max_tokens": int(cred.ai.get("max_tokens") or 1000),
    }
    if not cfg["api_key"]:
        print("[错误] 未配置 LLM API key（credentials.json 的 ai.api_key）")
        return 1

    with open(args.qa, encoding="utf-8") as f:
        cases = json.load(f)

    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(config.LOG_DIR, f"ai_reply_test_{ts}.md")
    lines = [
        "# AI 客服回复对照测试",
        "",
        f"- 模型：`{cfg['model']}`",
        "",
        "| 商品信息 | 买家问题 | 人工客服回复 | AI 回复 |",
        "|---|---|---|---|",
    ]

    for c in cases:
        q = c.get("question", "")
        human = c.get("human_answer", "") or ""
        product = c.get("product") or {}
        pinfo = build_product_info(product)
        ai = ai_reply.generate_reply(q, pinfo, cfg) or "(生成失败)"

        print(f"\n[商品] {product.get('title', '')}\n[问题] {q}\n[人工] {human}\n[AI] {ai}")
        lines.append(f"| {_md(pinfo)} | {_md(q)} | {_md(human)} | {_md(ai)} |")

    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n[报告] {out}")
    return 0
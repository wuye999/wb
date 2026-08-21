# -*- coding: utf-8 -*-
"""
wb_ops AI 自动回复（OpenAI 兼容 LLM）

调任意 OpenAI 兼容接口（DeepSeek / 商汤日日新 SenseNova / 通义千问 / OpenAI 等）
为买家提问生成俄语回复（后台 AI 模式用）。
内置 system prompt：礼貌、简短、基于商品信息作答、不编造、不确定引导客服。

配置在 data/credentials.json 的 ai 字段：
  { "base_url": "https://api.deepseek.com/chat/completions",
    "model": "deepseek-chat", "api_key": "sk-xxx" }
换成商汤日日新：base_url="https://token.sensenova.cn/v1/chat/completions"、model="sensenova-6.8-flash-lite"。
"""
import requests

DEFAULT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = (
    "你是俄罗斯电商平台 Wildberries 的卖家客服。请用俄语礼貌、简洁地回答买家关于商品的问题。"
    "严格基于提供的商品信息作答，不要编造信息；"
    "涉及物流、售后、退换货等具体政策且信息不足时，引导买家联系卖家客服。"
    "只输出回复正文，不要任何解释、前缀或引号。"
)


def generate_reply(question, product_info, cfg):
    """调 OpenAI 兼容 LLM 生成俄语回复。返回回复文本；失败（无 key/网络/异常）返回 None。
    cfg = {"base_url", "model", "api_key"}。"""
    key = (cfg or {}).get("api_key")
    if not key:
        return None
    base_url = cfg.get("base_url") or DEFAULT_URL
    model = cfg.get("model") or DEFAULT_MODEL
    user = f"商品信息：\n{product_info}\n\n买家问题：\n{question}"
    try:
        resp = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.6,
                "max_tokens": 500,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return content.strip() or None
    except Exception:
        return None

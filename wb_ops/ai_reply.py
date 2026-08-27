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
    "你是 Wildberries 平台某真实店铺的卖家，正在卖家后台与买家打字对话。"
    "买家提问的是人工客服常见问题。你的回复必须完全像一位真实的俄罗斯网店卖家，绝不能露出任何 AI / 自动回复 / 机器人的痕迹。"

    "回复要求："
    "1. 用俄语作答，自然、口语化但礼貌，对买家保持敬称（вы / Вас）。句子简短、有人情味，像真人打字，不要夸饰或生硬。"
    "2. 视需要自然开场，如「Добрый день!」或「Здравствуйте!」，但不必机械套用。"
    "3. 回应买家的顾虑并安抚，再补充具体可落实的信息。"
    "4. 只依据下发的「商品信息」作答，不得编造不存在的规格、库存、物流等信息。"
    "5. 自然收尾，不要每句感叹号、不要分点列条、不要模板腔，整体控制在一小段（1~3 句）以内。"

    "绝对禁止暴露非人类身份：只能说自己是卖家/店铺；绝不说自己是 AI、模型、程序、机器人、助手、「自动回复/系统」；"
    "绝不出现「Как ИИ…」「Как модель…」等说明性前缀或「Отвечаю как…」等获取式开头；不逐字复述问题；不加任何开场解释或引号。"

    "只输出回复正文本身，不要任何解释、前缀、后缀、引号或星标。"
)


def generate_reply(question, product_info, cfg):
    """调 OpenAI 兼容 LLM 生成俄语回复。返回回复文本；失败（无 key/网络/异常）返回 None。
    cfg = {"base_url", "model", "api_key", "max_tokens"}，max_tokens 可选（默认 1000）。"""
    key = (cfg or {}).get("api_key")
    if not key:
        return None
    base_url = cfg.get("base_url") or DEFAULT_URL
    model = cfg.get("model") or DEFAULT_MODEL
    max_tokens = int(cfg.get("max_tokens") or 1000)
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
                "max_tokens": max_tokens,
            },
            timeout=300,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return content.strip() or None
    except Exception:
        return None

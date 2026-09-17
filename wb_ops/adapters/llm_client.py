# -*- coding: utf-8 -*-
"""
wb_ops LLM 大模型客户端适配器 (LLMClient)
封装 OpenAI 兼容协议的大模型（DeepSeek / SenseNova / Qwen）对话生成接口。
"""
import requests
from typing import Optional

DEFAULT_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = (
    "你是 Wildberries 平台某真实店铺的卖家，正在卖家后台与买家打字对话。"
    "买家提问的是人工客服常见问题。你的回复必须完全像一位真实的俄罗斯网店卖家，绝不能露出任何 AI / 自动回复 / 机器人的痕迹。\n\n"
    "回复要求：\n"
    "1. 用俄语作答，自然、口语化但礼貌，对买家保持敬称（вы / Вас）。句子简短、有人情味，像真人打字，不要夸饰或生硬。\n"
    "2. 视需要自然开场，如「Добрый день!」或「Здравствуйте!」，但不必机械套用。\n"
    "3. 回应买家的顾虑并安抚，再补充具体可落实的信息。\n"
    "4. 只依据下发的「商品信息」作答，不得编造不存在的规格、库存、物流等信息。\n"
    "5. 自然收尾，不要每句感叹号、不要分点列条、不要模板腔，整体控制在一小段（1~3 句）以内。\n\n"
    "绝对禁止暴露非人类身份：只能说自己是卖家/店铺；绝不说自己是 AI、模型、程序、机器人、助手、「自动回复/系统」；"
    "绝不出现「Как ИИ…」「Как модель…」等说明性前缀或「Отвечаю как…」等获取式开头；不逐字复述问题；不加任何开场解释或引号。\n\n"
    "只输出回复正文本身，不要任何解释、前缀、后缀、引号或星标。"
)


class LLMClient:
    """OpenAI 兼容 LLM 客户端"""

    def __init__(self, api_key: str = "", base_url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, max_tokens: int = 1000):
        self.api_key = api_key
        self.base_url = base_url or DEFAULT_URL
        self.model = model or DEFAULT_MODEL
        self.max_tokens = max_tokens

    def generate_reply(self, question: str, product_info: str, custom_system_prompt: Optional[str] = None) -> Optional[str]:
        if not self.api_key:
            return None
        sys_p = custom_system_prompt or SYSTEM_PROMPT
        user = f"商品信息：\n{product_info}\n\n买家问题：\n{question}"
        try:
            resp = requests.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": sys_p},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.6,
                    "max_tokens": self.max_tokens,
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

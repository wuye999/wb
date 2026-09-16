# -*- coding: utf-8 -*-
"""
wb_ops 客服与智能应答用例服务 (CustomerSupportService)
统一管理买家提问拉取、自动轮询监控、大模型生成回复与提交。
"""
import os
import time
import datetime
from typing import List, Dict, Any, Optional, Set

from .. import config, common, credentials
from ..framework.safe_io import atomic_dump_json, safe_load_json
from ..adapters.llm_client import LLMClient

REPLIED_JSON = os.path.join(config.STATE_DIR, "questions_replied.json")
SHOWN_JSON = os.path.join(config.STATE_DIR, "questions_front_shown.json")


class CustomerSupportService:
    """客服与智能监控服务"""

    def __init__(self):
        cred = credentials.get()
        self.llm_client = LLMClient(
            api_key=cred.ai_key,
            base_url=cred.ai_base_url,
            model=cred.ai_model,
            max_tokens=cred.ai_max_tokens,
        )

    @staticmethod
    def load_replied() -> Set[str]:
        data = safe_load_json(REPLIED_JSON, default=[], use_lock=True)
        return set(data) if isinstance(data, list) else set()

    @staticmethod
    def save_replied(ids: Set[str]):
        atomic_dump_json(REPLIED_JSON, sorted(list(ids)), indent=2, use_lock=True)

    @staticmethod
    def load_shown() -> Set[str]:
        data = safe_load_json(SHOWN_JSON, default=[], use_lock=True)
        return set(data) if isinstance(data, list) else set()

    @staticmethod
    def save_shown(ids: Set[str]):
        atomic_dump_json(SHOWN_JSON, sorted(list(ids)), indent=2, use_lock=True)

    def generate_ai_reply(self, question: str, product_info: str) -> Optional[str]:
        """调用大模型为提问生成专业回复"""
        return self.llm_client.generate_reply(question, product_info)

    def list_and_answer_questions(self, args: Any) -> int:
        """运行客服提问查询与回答"""
        from .support import questions
        return questions.run(args)

    def watch_and_auto_reply(self, args: Any) -> int:
        """运行买家提问自动轮询监控"""
        from .support import questions_watch
        return questions_watch.run(args)

    def test_ai_dialog(self, args: Any) -> int:
        """测试大模型客服对话应答"""
        from .support import ai_reply_test
        return ai_reply_test.run(args)


support_svc = CustomerSupportService()

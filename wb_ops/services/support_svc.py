# -*- coding: utf-8 -*-
"""
wb_ops 客服与智能应答用例服务 (CustomerSupportService)
统一管理买家提问拉取、自动轮询监控、大模型生成回复与提交。

注：提问「已回复 / 已展示」状态文件的读写唯一实现位于 services/support/questions_watch.py
（该状态的写入方就是轮询进程），本模块不再重复实现，避免两处状态逻辑漂移。
"""
from typing import Any, Optional

from .. import common, credentials
from ..adapters.llm_client import LLMClient


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

    def list_pending_appeals(self, args: Any) -> int:
        """运行 WB 平台投诉单查询（只读：未处理 + 剩余天数筛选）"""
        from .support import complaints
        return complaints.run(args)


support_svc = CustomerSupportService()


def run_questions(args):
    return support_svc.list_and_answer_questions(args)


def run_questions_watch(args):
    return support_svc.watch_and_auto_reply(args)


def run_ai_test(args):
    return support_svc.test_ai_dialog(args)


def run_appeals(args):
    return support_svc.list_pending_appeals(args)


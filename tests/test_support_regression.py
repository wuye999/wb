"""Offline business regression cases with synthetic fixtures."""
import os
import time
import unittest


class TestRegression(unittest.TestCase):
    @staticmethod
    def _fb(found=None, stats=None, missing=None, pages=1):
            """构造 find_batches 返回值（离线单测用）。"""
            found = found or {}
            return {"found": found, "missing": set(missing or []),
                    "all_found": not (missing or []), "pages_fetched": pages,
                    "stats": stats or {}}

    def test_chatwatch_payload_offline(self):
        """chat-watch 离线断言（**不联网**）：发送体构造 + 上下文轮次整理 + user prompt 组装。

            发送体关键字段（HAR 实测）：sign=会话级 sellerSign、sender.name=店铺名、
            recipients[0].name=买家名、addTime=clientCreatedAt=当前毫秒。
            """
        from sellerops.integrations.wildberries import chats as cc
        from sellerops.features.support import chat_prompts as cp
        captured = {}
        orig = cc.wb_api.request

        def _fake(session, method, url, **kw):
            captured.update(method=method, url=url, json=kw.get('json'))
            return {'chatID': 'C1', 'sign': 'SIGN'}
        try:
            cc.wb_api.request = _fake
            d = cc.send_message(object(), 'C1', 'текст', 'Магазин', 'Ника', 'SIGN', now_ms=123)
        finally:
            cc.wb_api.request = orig
        self.assertEqual(captured['method'], 'POST')
        self.assertTrue(captured['url'].endswith('/chat/v5/seller/send'))
        body = captured['json']
        self.assertEqual(body['chatID'], 'C1')
        self.assertEqual(body['sign'], 'SIGN')
        self.assertEqual(body['addTime'], 123)
        self.assertEqual(body['clientCreatedAt'], 123)
        self.assertEqual(body['sender'], {'name': 'Магазин', 'type': 'seller'})
        self.assertEqual(body['recipients'], [{'type': 'client', 'name': 'Ника'}])
        self.assertEqual(body['message']['text'], 'текст')
        self.assertEqual(body['message']['actionType'], 'create')
        self.assertEqual(d.get('chatID'), 'C1')
        cc.wb_api.request = lambda session, method, url, **kw: [{'chatID': 'C1'}]
        try:
            self.assertEqual(cc.list_chats(object()), [{'chatID': 'C1'}])
        finally:
            cc.wb_api.request = orig
        events = [{'isOwn': False, 'message': {'text': 'm3'}}, {'isOwn': True, 'message': {'text': 'm2'}}, {'isOwn': False, 'message': {'text': 'm1'}}]
        self.assertEqual(cp.recent_turns(events, max_turns=2), ['买家：m1', '卖家：m2'])
        self.assertEqual(cc.latest_event(events)['message']['text'], 'm3')
        self.assertEqual(cc.latest_seller_sign([{'sellerSign': 'SS'}]), 'SS')
        self.assertTrue(cc.chat_needs_reply({'lastEvent': {'isOwn': False}}))
        self.assertFalse(cc.chat_needs_reply({'lastEvent': {'isOwn': True}}))
        up = cp.build_user_prompt('商品信息X', '最新问题', cp.recent_turns(events, 2))
        self.assertIn('【需要你回复的买家最新消息】', up)
        self.assertIn('最新问题', up)
        self.assertIn('买家：m1', up)
        self.assertIn('绝不', cp.CHAT_SYSTEM_PROMPT)

# -*- coding: utf-8 -*-
"""
wb_ops WB 买家聊天(chat-with-clients)接口适配器

来源：api/网络请求/wb购买后消息处理.har（2026-10-14）。
Host `https://seller-chat.wildberries.ru`，业务前缀 `/ns/sellers-chat-srv-1`。
鉴权复用 `wb_client.make_session` 的 cookie 三件套（Origin/Referer=seller.wildberries.ru），
请求统一走 `wb_client.request`（403→CookieExpiredError；401→自动刷新重试；其余退避重试）。

关键事实（HAR 实测确认）：
- 会话列表响应「**顶层即数组**」。
- 消息历史 `events` 按时间**倒序**（`events[0]` 最新）。
- 发送消息的 `sign` = 会话级 `sellerSign`（history 每条事件都带；列表元素顶层 `sign` 亦相等，可兜底）。
- `unread` 查询参数语义未验证，调用方应以 `unreadCount` 字段为准。
"""
import time
from typing import Any, Dict, List, Optional

from . import wb_client as wb_api

CHAT_BASE = "https://seller-chat.wildberries.ru/ns/sellers-chat-srv-1"
CHATS_URL = CHAT_BASE + "/chat/v4/seller/chats"
HISTORY_URL = CHAT_BASE + "/chat/v2/seller/chat/history"
SEND_URL = CHAT_BASE + "/chat/v5/seller/send"
UNREAD_URL = CHAT_BASE + "/chat/v2/seller/unread"

BUYER_CHAT_TYPE = 1   # 1=买家聊天（有数据）；3 实测返回空


def list_chats(session, chat_type: int = BUYER_CHAT_TYPE, limit: int = 20,
               unread: bool = False, is_active: bool = True) -> List[Dict[str, Any]]:
    """GET 会话列表（响应顶层即数组）。unread 参数语义未验证，建议调用方按 unreadCount 过滤。"""
    url = (f"{CHATS_URL}?addTime=0&chatType={chat_type}"
           f"&isActive={'true' if is_active else 'false'}"
           f"&limit={int(limit)}&unread={'true' if unread else 'false'}")
    d = wb_api.request(session, "GET", url)
    return d if isinstance(d, list) else []


def fetch_history(session, chat_id: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """POST 会话消息历史。返回 {totalCount, events[]}，events 按时间倒序（[0] 最新）。"""
    d = wb_api.request(session, "POST", HISTORY_URL,
                       json={"chatID": chat_id, "limit": int(limit), "offset": int(offset)})
    return d if isinstance(d, dict) else {}


def send_message(session, chat_id: str, text: str, seller_name: str, client_name: str,
                 sign: str, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """POST 发送消息（回复买家）。

    Args:
        seller_name: 该会话 `seller.name`（非 credentials 的 shopName）。
        client_name: 该会话 `client.name`。
        sign: 会话级 sellerSign（见模块说明）。
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    payload = {
        "chatID": chat_id,
        "addTime": now_ms,
        "clientCreatedAt": now_ms,
        "eventType": "message",
        "recipients": [{"type": "client", "name": client_name}],
        "sign": sign,
        "sender": {"name": seller_name, "type": "seller"},
        "message": {"actionType": "create",
                    "attachments": {"images": [], "files": []},
                    "text": text},
    }
    d = wb_api.request(session, "POST", SEND_URL, json=payload)
    return d if isinstance(d, dict) else {}


def fetch_unread(session, chat_type: str = "1,2,3") -> Dict[str, Any]:
    """GET 未读计数（发送前后对比，验证「回完是否清零」）。"""
    d = wb_api.request(session, "GET", f"{UNREAD_URL}?chatType={chat_type}")
    return d if isinstance(d, dict) else {}


# ---------------- 事件/会话解析工具（纯函数，便于离线单测） ----------------

def latest_event(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """events 倒序 → [0] 即最新事件。"""
    return events[0] if events else None


def latest_seller_sign(events: List[Dict[str, Any]]) -> str:
    """取任一事件的 sellerSign（全会话常量）。"""
    for e in events or []:
        s = (e or {}).get("sellerSign")
        if s:
            return s
    return ""


def event_is_own(event: Optional[Dict[str, Any]]) -> bool:
    """该事件是否卖家自己发的。"""
    return bool((event or {}).get("isOwn"))


def event_text(event: Optional[Dict[str, Any]]) -> str:
    return ((event or {}).get("message") or {}).get("text", "") or ""


def event_time(event: Optional[Dict[str, Any]]) -> int:
    try:
        return int((event or {}).get("addTime") or 0)
    except (TypeError, ValueError):
        return 0


def extract_good_card(obj: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从会话/事件对象取 goodCard。"""
    return (obj or {}).get("goodCard") or {}


def chat_needs_reply(chat: Dict[str, Any]) -> bool:
    """列表层判定：最新一条消息来自买家（lastEvent.isOwn 为假）则可能需要回复。"""
    return not event_is_own((chat or {}).get("lastEvent"))

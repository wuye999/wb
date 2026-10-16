# -*- coding: utf-8 -*-
"""
wb_ops WB 买家聊天会话查询（只读命令 `chats`）

用途：上线前验证 seller-chat 域 cookie 连通性、查看会话列表与消息历史、核对商品信息。
零写操作：不发送、不标记已读、不改任何平台状态。

来源与事实：见 adapters/chat_client.py 模块说明（HAR 实测）。
商品信息复用问答版同款拼装（nmId→vendorCode→中文名 + basket card.json）。
"""
import csv
import datetime
import os
import time
from typing import Any, Dict, List

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import chat_client as chat_api
from wb_ops.adapters import wb_client as wb_api
from wb_ops.services.support.questions_watch import product_info_str
from wb_ops.storage.mapping_repo import MappingRepository
from wb_ops.storage.nm_resolver import NmResolver

CSV_FIELDS = ["店铺", "店铺ID", "会话ID", "买家", "未读数", "最新消息时间", "最新消息",
              "nmId", "供应商代码", "中文名"]


def _fmt_time(ms: Any) -> str:
    try:
        ms = int(ms)
        if ms <= 0:
            return ""
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _parse_shops(cred, shops_arg: str):
    shops = cred.wb_shops()
    if shops_arg:
        want = {int(x) for x in shops_arg.split(",") if x.strip()}
        shops = [s for s in shops if common.to_int(s.get("shopId")) in want]
    return shops


def run(args) -> int:
    common.ensure_utf8_stdout()
    cred = credentials.get()
    shops = _parse_shops(cred, getattr(args, "shops", ""))
    if not shops:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）")
        return 1

    limit = common.to_int(getattr(args, "limit", 20)) or 20
    chat_id = getattr(args, "chat_id", "") or ""
    history_n = common.to_int(getattr(args, "history", 0))
    since_hours = common.to_int(getattr(args, "since_hours", 0))
    no_detail = bool(getattr(args, "no_detail", False))

    cn_map: Dict[str, Any] = {}
    try:
        cn_map, _ = MappingRepository.load_mapping_state()
    except Exception:
        pass

    names = ", ".join(f"{s['shopName']}({s['shopId']})" for s in shops)
    print(f"店铺 {len(shops)} 个: {names}（只读；chatType=1｜限制每店 {limit} 会话"
          f"{f'｜最近 {since_hours}h' if since_hours else '｜不限时间'}）")
    now_ms = int(time.time() * 1000)

    rows: List[Dict[str, Any]] = []
    total_chats = 0
    for shop in shops:
        sid = shop["shopId"]
        nm_resolver = NmResolver(sid, with_cn=not no_detail)
        try:
            session = wb_api.make_session(shop, cred.root_version)
            chats = chat_api.list_chats(session, limit=limit, unread=False)
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {shop['shopName']}: {e}")
            continue
        except Exception as e:
            print(f"  [警告] 店铺 {shop['shopName']} 拉取会话失败: {e}")
            continue

        if chat_id:
            chats = [c for c in chats if c.get("chatID") == chat_id]
            if not chats:
                print(f"\n=== {shop['shopName']}({sid}) 会话列表中未找到 chatID={chat_id}"
                      f"（可能不在前 {limit} 条内）===")
                continue
        elif since_hours > 0:
            chats = [c for c in chats
                     if now_ms - common.to_int((c.get("lastMessage") or {}).get("time")) <= since_hours * 3600 * 1000]

        total_chats += len(chats)
        print(f"\n=== {shop['shopName']}({sid}) 会话 {len(chats)} 个 ===")
        for c in chats:
            chat_cid = c.get("chatID") or ""
            client_name = (c.get("client") or {}).get("name") or ""
            unread = common.to_int(c.get("unreadCount"))
            last_msg = (c.get("lastMessage") or {})
            last_text = last_msg.get("text", "") or ""
            last_time = _fmt_time(last_msg.get("time"))
            needs = "是" if chat_api.chat_needs_reply(c) else "否"
            gc = chat_api.extract_good_card(c)
            nm_id = gc.get("nmID") or gc.get("nmId")
            info = nm_resolver.resolve(nm_id)
            print(f"  · {chat_cid} | 买家:{client_name} | 未读:{unread} | 待回复:{needs}"
                  f" | {last_time} | {last_text[:60]}")

            # 会话历史（--history N 或指定 --chat-id 时默认 10 条）
            show_n = history_n or (10 if chat_id else 0)
            if show_n > 0:
                try:
                    hist = chat_api.fetch_history(session, chat_cid, limit=max(show_n, 1))
                except Exception as e:
                    print(f"      [警告] 拉取历史失败: {e}")
                    hist = {}
                events = (hist or {}).get("events") or []
                for ev in reversed(events[-show_n:]):     # 展示为旧→新
                    who = "卖家" if ev.get("isOwn") else "买家"
                    print(f"      [{who}] {_fmt_time(ev.get('addTime'))} "
                          f"{chat_api.event_text(ev)[:120]}")

            # 商品信息（细化）
            prod = ""
            if not no_detail:
                prod = product_info_str(info["vc"], nm_id, cn_map)
                if prod and prod != "(无商品信息)":
                    print(f"      商品: {prod.replace(chr(10), ' | ')}")

            rows.append({
                "店铺": shop["shopName"], "店铺ID": sid, "会话ID": chat_cid,
                "买家": client_name, "未读数": unread,
                "最新消息时间": last_time, "最新消息": last_text,
                "nmId": nm_id or "", "供应商代码": info["vc"] or "",
                "中文名": info["cn"] or "",
            })
            time.sleep(0.2)
        time.sleep(0.5)

    print(f"\n[汇总] 会话 {total_chats} 个；明细 {len(rows)} 行")
    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"买家聊天_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")
    return 0

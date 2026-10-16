# -*- coding: utf-8 -*-
"""
wb_ops AI 售后客服：买家聊天实时监听（前台AI / 后台AI 双模式）

常驻循环：每 interval 秒查各店「有未读的会话」，对「最新一条来自买家」的消息处置。

- 前台AI模式（front，默认）：只把「买家消息 + 商品信息 + 建议回复」打印到控制台并追加日志，
  不生成、不发送；由前台人工拟定并回复。
- 后台AI模式（back，--apply 或 --mode back）：用售后客服提示词让 LLM 生成俄语回复并自动发送。

防重复（键 = `shopId|chatID` → 最新处理的买家 eventID）：
- 前台：data/state/chat_front_shown.json（已展示集合）。
- 后台：data/state/chat_replied.json（**仅 --apply 且发送成功才写**）。

口径（用户 2026-10-14 确认，含联网实测修正）：只回复「最新一条来自买家」的会话消息，
并叠加时间窗（默认最近 72 小时，--since-hours 可调；0=不限）避免首次上线补回历史旧账。
⚠ 平台未读标记不可信：实测所有会话 `unreadCount` 恒为 0、unread 接口 `messageCount=0`，
故**不以「未读」为闸门**，改用「最后一条来自买家 + eventID 去重 + 时间窗」三件套。
回复策略=不暴露 AI、软性拒退换、其余问题模糊/拖延（见 support/chat_prompts.py）。
"""
import csv
import datetime
import json
import os
import time

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import chat_client as chat_api
from wb_ops.adapters import wb_client as wb_api
from wb_ops.adapters.llm_client import LLMClient
from wb_ops.framework.safe_io import safe_load_json, atomic_dump_json
from wb_ops.services.support.chat_prompts import (CHAT_SYSTEM_PROMPT, build_user_prompt,
                                                  recent_turns)
from wb_ops.services.support.questions_watch import product_info_str
from wb_ops.storage.mapping_repo import MappingRepository
from wb_ops.storage.nm_resolver import NmResolver

REPLIED_JSON = os.path.join(config.STATE_DIR, "chat_replied.json")
SHOWN_JSON = os.path.join(config.STATE_DIR, "chat_front_shown.json")


def load_state(path):
    data = safe_load_json(path, default={}, use_lock=True)
    return data if isinstance(data, dict) else {}


def save_state(path, data):
    atomic_dump_json(path, data, indent=2, use_lock=True)


def load_replied():
    return load_state(REPLIED_JSON)


def save_replied(d):
    save_state(REPLIED_JSON, d)


def load_shown():
    return load_state(SHOWN_JSON)


def save_shown(d):
    save_state(SHOWN_JSON, d)


def _state_key(shop_id, chat_id):
    return f"{shop_id}|{chat_id}"


def _display_block(shop, chat, prod, buyer_msg, now, front_log):
    """前台模式：打印并写日志一条待回复的买家消息。"""
    chat_id = chat.get("chatID")
    client_name = (chat.get("client") or {}).get("name") or ""
    unread = common.to_int(chat.get("unreadCount"))
    block = "\n".join([
        f"\n[{now}] 店{shop['shopId']} 会话 {chat_id}",
        f"  商品: {prod.replace(chr(10), chr(10) + '        ')}",
        f"  买家: {client_name}（未读 {unread}）",
        f"  买家最新消息: {buyer_msg}",
    ])
    print(block)
    if front_log:
        front_log.write(block + "\n")
        front_log.flush()


def poll_once(args, cn_map, reply_cfg, replied, shown, csv_writer, front_log):
    """一轮：遍历各店有未读的会话，处置「最新一条来自买家」的消息。返回本店累计发送数。"""
    cred = credentials.get()
    shops = cred.wb_shops()
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if common.to_int(s.get("shopId")) in want]
    root_version = cred.root_version
    front = (args.mode != "back")
    limit = common.to_int(getattr(args, "limit", 20)) or 20
    history_limit = common.to_int(getattr(args, "history_limit", 50)) or 50
    max_turns = common.to_int(getattr(args, "max_turns", 3))
    since_hours = common.to_int(getattr(args, "since_hours", 72))

    replied_n = 0
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ms = int(time.time() * 1000)
    for shop in shops:
        sid = shop["shopId"]
        resolver = NmResolver(sid)
        try:
            s = wb_api.make_session(shop, root_version)
            chats = chat_api.list_chats(s, limit=limit, unread=False)
        except common.CookieExpiredError as e:
            print(f"  [警告] 店{sid} cookie 失效: {e}")
            continue
        except Exception as e:
            print(f"  [警告] 店{sid} 拉取会话失败: {e}")
            continue

        for chat in chats:
            chat_id = chat.get("chatID")
            if not chat_id:
                continue
            if not chat_api.chat_needs_reply(chat):
                continue
            try:
                hist = chat_api.fetch_history(s, chat_id, limit=history_limit)
            except common.CookieExpiredError as e:
                print(f"  [警告] 店{sid} 会话 {chat_id} cookie 失效，该店中止: {e}")
                break
            except Exception as e:
                print(f"  [警告] 店{sid} 会话 {chat_id} 拉取历史失败: {e}")
                continue
            events = (hist or {}).get("events") or []
            latest = chat_api.latest_event(events)
            if not latest or chat_api.event_is_own(latest):
                continue
            event_id = latest.get("eventID")
            if not event_id:
                print(f"  [跳过] 店{sid} 会话 {chat_id} 最新消息无 eventID")
                continue
            if since_hours > 0:
                age_ms = now_ms - chat_api.event_time(latest)
                if age_ms > since_hours * 3600 * 1000:
                    continue   # 超出时间窗（默认 72h），不翻旧账

            store = shown if front else replied
            key = _state_key(sid, chat_id)
            if store.get(key) == event_id:
                continue

            seller_name = (chat.get("seller") or {}).get("name") or shop["shopName"]
            client_name = (chat.get("client") or {}).get("name") or ""
            sign = chat_api.latest_seller_sign(events) or chat.get("sign") or ""
            gc = chat_api.extract_good_card(latest) or chat_api.extract_good_card(chat)
            nm_id = gc.get("nmID") or gc.get("nmId")
            info = resolver.resolve(nm_id)
            prod = product_info_str(info["vc"] if info else "", nm_id, cn_map)
            buyer_msg = chat_api.event_text(latest)

            if front:
                _display_block(shop, chat, prod, buyer_msg, now, front_log)
                shown[key] = event_id
                save_shown(shown)
                continue

            # ---- 后台AI模式 ----
            recent = recent_turns(events, max_turns=max_turns)
            user_p = build_user_prompt(prod, buyer_msg, recent)
            print(f"\n[新消息] 店{sid} 会话 {chat_id} 买家:{client_name}")
            print(f"  商品: {prod}")
            print(f"  买家: {buyer_msg}")
            reply_text = reply_cfg.generate_messages(CHAT_SYSTEM_PROMPT, user_p) if reply_cfg else None
            if not reply_text:
                print("  [跳过] AI 生成回复失败（接口异常）")
                _csv_row(csv_writer, now, sid, chat_id, nm_id, buyer_msg, "", "生成失败")
                continue
            print(f"  [AI 回复] {reply_text}")
            if not sign:
                print("  [跳过] 缺 sellerSign，无法发送")
                _csv_row(csv_writer, now, sid, chat_id, nm_id, buyer_msg, reply_text, "无sign失败")
                continue

            ok = False
            result = "dry-run"
            if args.apply:
                try:
                    d = chat_api.send_message(s, chat_id, reply_text, seller_name, client_name, sign)
                    ok = (d.get("chatID") == chat_id)
                    result = "已发送" if ok else f"发送失败:{d.get('errorText') or '未知'}"
                except Exception as e:
                    result = f"发送失败:{str(e)[:60]}"
                print(f"  >> {result}")
                if ok:
                    replied[key] = event_id
                    save_replied(replied)
                    replied_n += 1
            else:
                print("  [dry-run] 未发送")
            _csv_row(csv_writer, now, sid, chat_id, nm_id, buyer_msg, reply_text, result)
            time.sleep(1.0)
        time.sleep(0.5)
    return replied_n


def _csv_row(csv_writer, now, sid, chat_id, nm_id, buyer_msg, reply_text, result):
    if csv_writer is not None:
        csv_writer.writerow([now, sid, chat_id, nm_id or "", buyer_msg, reply_text, result])


def run(args) -> int:
    common.ensure_utf8_stdout()
    # 解析模式：--apply（未显式 --mode）或 --mode back → 后台AI；否则前台AI（默认）
    if getattr(args, "mode", "") != "back" and getattr(args, "apply", False):
        args.mode = "back"
    front = (getattr(args, "mode", "front") != "back")
    cred = credentials.get()
    interval = args.interval or cred.ai_watch_interval
    since_hours = common.to_int(getattr(args, "since_hours", 72))
    win_txt = f"最近 {since_hours}h 内" if since_hours else "不限时间"

    cn_map = {}
    try:
        cn_map, _ = MappingRepository.load_mapping_state()
    except Exception:
        pass

    if front:
        print(f"监听启动（前台AI/展示+手动）：间隔 {interval}s，检测「{win_txt}最新一条来自买家」的消息，不自动发送")
        shown = load_shown()
        os.makedirs(config.LOG_DIR, exist_ok=True)
        front_log = open(os.path.join(
            config.LOG_DIR, f"chat_watch_front_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
            "a", encoding="utf-8")
        print(f"  日志: {front_log.name}")
        if args.once:
            poll_once(args, cn_map, None, {}, shown, None, front_log)
            front_log.close()
            print("（--once 单轮结束）")
            return 0
        try:
            while True:
                try:
                    poll_once(args, cn_map, None, {}, shown, None, front_log)
                except Exception as e:
                    print(f"  [轮次异常] {e}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n已停止监听")
        finally:
            front_log.close()
        return 0

    # 后台AI模式
    cfg = LLMClient(api_key=cred.ai_key, base_url=cred.ai_base_url,
                    model=cred.ai_model, max_tokens=cred.ai_max_tokens)
    if args.apply and not cfg.api_key:
        print("[错误] 后台模式 --apply 需要配置 LLM API key（credentials.json 的 ai.api_key）")
        return 1
    replied = load_replied()

    os.makedirs(config.LOG_DIR, exist_ok=True)
    csv_path = os.path.join(config.LOG_DIR,
                            f"chat_watch_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(["时间", "店铺", "会话ID", "nmId", "买家消息", "AI回复", "结果"])

    print(f"监听启动（后台AI/自动发送）：间隔 {interval}s，窗口 {win_txt}，模式 "
          f"{'apply（自动发送）' if args.apply else 'dry-run（只打草稿）'}，模型 {cfg.model}，"
          f"已发送 {len(replied)} 条")
    if args.once:
        poll_once(args, cn_map, cfg, replied, {}, csv_writer, None)
        csv_file.close()
        print("（--once 单轮结束）")
        return 0

    try:
        while True:
            try:
                poll_once(args, cn_map, cfg, replied, {}, csv_writer, None)
                csv_file.flush()
            except Exception as e:
                print(f"  [轮次异常] {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止监听")
    finally:
        csv_file.close()
    return 0

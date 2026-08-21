# -*- coding: utf-8 -*-
"""
wb_ops 买家提问实时监听（后台 AI 模式）

常驻循环：每 interval 秒查各店未处理提问 → 过滤已回复 id → 查商品信息
→ DeepSeek 生成俄语回复 → 提交（--apply）并记录。
记录已回复 id（data/state/questions_replied.json），防重复回复。
"""
import csv
import datetime
import json
import os
import time

from . import ai_reply
from . import common
from . import config
from . import credentials
from . import mapping
from . import questions
from . import replicate
from . import wb_api

REPLIED_JSON = os.path.join(config.STATE_DIR, "questions_replied.json")


def load_replied():
    try:
        return set(json.load(open(REPLIED_JSON, encoding="utf-8")))
    except Exception:
        return set()


def save_replied(s):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    json.dump(sorted(s), open(REPLIED_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def product_info_str(vc, nm_id, cn_map):
    """构造商品信息字符串（供 AI 参考）：中文名 + card.json 描述/特征 + detail 品牌/价格/颜色。"""
    cn = cn_map.get(vc, {}).get("cn", "") if vc else ""
    parts = [f"中文名：{cn}"] if cn else []
    if nm_id:
        info = replicate.fetch_product_info(nm_id)
        if info.get("title"):
            parts.append(f"标题：{info['title']}")
        if info.get("brand"):
            parts.append(f"品牌：{info['brand']}")
        if info.get("colors"):
            parts.append(f"颜色：{info['colors']}")
        if info.get("price"):
            parts.append(f"价格：{info['price']}")
        if info.get("description"):
            parts.append(f"商品描述：{info['description']}")
        if info.get("options"):
            parts.append(f"商品特征：{info['options']}")
    return "\n".join(parts) if parts else "(无商品信息)"


def poll_once(args, cn_map, cfg, replied, csv_writer):
    """一轮：查各店未处理提问，对新提问 AI 回复并（可选）提交。返回本轮提交数。"""
    cred = credentials.get()
    shops = cred.wb_shops()
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if s["shopId"] in want]
    root_version = cred.root_version
    replied_n = 0
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for shop in shops:
        try:
            s = wb_api.make_session(shop, root_version)
            qs = questions.fetch_questions(s)
            for q in qs:
                qid = q.get("id")
                if not qid or qid in replied:
                    continue
                qinfo = q.get("questionInfo") or {}
                pinfo = q.get("productInfo") or {}
                vc = pinfo.get("supplierArticle") or ""
                nm_id = pinfo.get("wbArticle")
                text = qinfo.get("text", "")
                pinfo_str = product_info_str(vc, nm_id, cn_map)
                print(f"\n[新提问] 店{shop['shopId']} {qid}\n  商品: {pinfo_str}\n  问题: {text}")
                reply_text = ai_reply.generate_reply(text, pinfo_str, cfg)
                if not reply_text:
                    print("  [跳过] AI 生成回复失败（无 key 或接口异常）")
                    continue
                print(f"  [AI 回复] {reply_text}")
                if args.apply:
                    d = questions.reply(s, qid, reply_text)
                    ok = not d.get("error")
                    print(f"  >> {'已提交' if ok else '提交失败: ' + str(d.get('errorText'))}")
                    if ok:
                        replied.add(qid)
                        replied_n += 1
                else:
                    print("  [dry-run] 未提交")
                if csv_writer is not None:
                    csv_writer.writerow([now, shop["shopId"], qid, vc, text, reply_text,
                                         "已提交" if (args.apply and ok) else "dry-run"])
                time.sleep(1.0)
        except common.CookieExpiredError as e:
            print(f"  [警告] 店{shop['shopId']} cookie 失效: {e}")
        except Exception as e:
            print(f"  [警告] 店{shop['shopId']} 处理失败: {e}")
        time.sleep(0.5)
    if replied_n:
        save_replied(replied)
    return replied_n


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    cfg = {"base_url": cred.ai_base_url, "model": cred.ai_model, "api_key": cred.ai_key}
    if args.apply and not cfg["api_key"]:
        print("[错误] 后台模式 --apply 需要配置 LLM API key（credentials.json 的 ai.api_key）")
        return 1
    interval = args.interval or cred.ai_watch_interval

    cn_map = {}
    try:
        cn_map = mapping.load_mapping_state()[0]
    except Exception:
        pass
    replied = load_replied()

    csv_path = os.path.join(config.LOG_DIR, f"questions_watch_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(config.LOG_DIR, exist_ok=True)
    csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(["时间", "店铺", "提问ID", "供应商编码", "买家问题", "AI回复", "结果"])

    print(f"监听启动：间隔 {interval}s，模式 {'apply（自动提交）' if args.apply else 'dry-run（只打草稿）'}，"
          f"模型 {cfg['model']}，已回复 {len(replied)} 条")
    if args.once:
        poll_once(args, cn_map, cfg, replied, csv_writer)
        csv_file.close()
        print("（--once 单轮结束）")
        return 0

    try:
        while True:
            try:
                poll_once(args, cn_map, cfg, replied, csv_writer)
                csv_file.flush()
            except Exception as e:
                print(f"  [轮次异常] {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止监听")
    finally:
        csv_file.close()
    return 0

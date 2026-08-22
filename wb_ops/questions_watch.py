# -*- coding: utf-8 -*-
"""
wb_ops 买家提问实时监听（前台AI / 后台AI 双模式）

常驻循环：每 interval 秒查各店未处理提问。

- 前台AI模式（front，默认）：只把「提问 + 商品信息 + 可复制回复命令」打印到控制台并追加写日志，
  不提交、不用 LLM。由前台读取控制台/日志，自行拟定回复内容，再用 `wb.py questions --reply ... --question-id <id>` 手动提交。
- 后台AI模式（back，--apply 或 --mode back）：对接 OpenAI 兼容模型（DeepSeek/商汤等）自动生成俄语回复并自动提交，前台不参与。

防重复：
- 前台模式：只写独立记录 data/state/questions_front_shown.json（「已展示」集合），避免每次轮询重复打印；不写 replied（它不负责回复）。
- 后台模式：把提交成功的 id 写入 data/state/questions_replied.json，防重复回复。
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
SHOWN_JSON = os.path.join(config.STATE_DIR, "questions_front_shown.json")


def _load_ids(path):
    try:
        return set(json.load(open(path, encoding="utf-8")))
    except Exception:
        return set()


def _save_ids(path, s):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    json.dump(sorted(s), open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_replied():
    return _load_ids(REPLIED_JSON)


def save_replied(s):
    _save_ids(REPLIED_JSON, s)


def load_shown():
    return _load_ids(SHOWN_JSON)


def save_shown(s):
    _save_ids(SHOWN_JSON, s)


def product_info_str(vc, nm_id, cn_map):
    """构造商品信息字符串（供查看/后台 AI 参考）：中文名 + card.json 描述/特征/颜色 + 价格映射表店铺价(CNY)。"""
    cn = cn_map.get(vc, {}).get("cn", "") if vc else ""
    parts = [f"中文名：{cn}"] if cn else []
    if nm_id:
        info = replicate.fetch_product_info(nm_id, vc=vc, own=cn_map)
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


def _display_new_question(shop, q, cn_map, front_log):
    """前台模式：打印并写日志一条新提问 + 可复制回复命令。返回是否成功处理。"""
    qinfo = q.get("questionInfo") or {}
    pinfo = q.get("productInfo") or {}
    rtime = q.get("responseTime") or {}
    qid = q.get("id")
    vc = pinfo.get("supplierArticle") or ""
    nm_id = pinfo.get("wbArticle")
    text = qinfo.get("text", "") or ""
    user = qinfo.get("userName", "") or ""
    deadline = rtime.get("deadlineDate", "") or ""
    info = product_info_str(vc, nm_id, cn_map)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cmd = f'python wb.py questions --reply "回复内容" --question-id {qid}'
    lines = [
        f"\n[{now}] 店{shop['shopId']} 新提问 id={qid}",
        f"  商品: {info.replace(chr(10), chr(10)+'        ')}",
        f"  提问人: {user}",
        f"  回复截止: {deadline}",
        f"  买家问题: {text}",
        f"  回复命令: {cmd}",
    ]
    block = "\n".join(lines)
    print(block)
    if front_log:
        front_log.write(block + "\n")
        front_log.flush()
    return True


def poll_once(args, cn_map, reply_cfg, replied, shown, csv_writer, front_log):
    """一轮：查各店未处理提问。front 模式只展示；back 模式生成并（--apply 时）提交。返回提交数。"""
    cred = credentials.get()
    shops = cred.wb_shops()
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if s["shopId"] in want]
    root_version = cred.root_version
    front = (args.mode != "back")
    replied_n = 0
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for shop in shops:
        try:
            s = wb_api.make_session(shop, root_version)
            qs = questions.fetch_questions(s)
            for q in qs:
                qid = q.get("id")
                if not qid:
                    continue
                if front:
                    if qid in shown:
                        continue
                    if _display_new_question(shop, q, cn_map, front_log):
                        shown.add(qid)
                        save_shown(shown)
                else:
                    if qid in replied:
                        continue
                    qinfo = q.get("questionInfo") or {}
                    pinfo = q.get("productInfo") or {}
                    vc = pinfo.get("supplierArticle") or ""
                    nm_id = pinfo.get("wbArticle")
                    text = qinfo.get("text", "")
                    pinfo_str = product_info_str(vc, nm_id, cn_map)
                    print(f"\n[新提问] 店{shop['shopId']} {qid}")
                    print(f"  商品: {pinfo_str}")
                    print(f"  问题: {text}")
                    reply_text = ai_reply.generate_reply(text, pinfo_str, reply_cfg)
                    if not reply_text:
                        print("  [跳过] AI 生成回复失败（接口异常）")
                        continue
                    print(f"  [AI 回复] {reply_text}")
                    ok = False
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
    # 解析模式：--apply（未显式 --mode）或 --mode back → 后台AI；否则前台AI（默认）
    if getattr(args, "mode", "") != "back" and getattr(args, "apply", False):
        args.mode = "back"
    front = (getattr(args, "mode", "front") != "back")
    cred = credentials.get()
    interval = args.interval or cred.ai_watch_interval

    if front:
        print(f"监听启动（前台AI/展示+手动）：间隔 {interval}s，检测新提问打印到控制台与日志，不自动提交")
        cn_map = {}
        try:
            cn_map = mapping.load_mapping_state()[0]
        except Exception:
            pass
        shown = load_shown()
        os.makedirs(config.LOG_DIR, exist_ok=True)
        front_log = open(os.path.join(
            config.LOG_DIR, f"questions_front_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
            "a", encoding="utf-8")
        print(f"  日志: {front_log.name}")
        if args.once:
            poll_once(args, cn_map, None, set(), shown, None, front_log)
            front_log.close()
            print("（--once 单轮结束）")
            return 0
        try:
            while True:
                try:
                    poll_once(args, cn_map, None, set(), shown, None, front_log)
                except Exception as e:
                    print(f"  [轮次异常] {e}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n已停止监听")
        finally:
            front_log.close()
        return 0

    # 后台AI模式（原全自动）
    cfg = {"base_url": cred.ai_base_url, "model": cred.ai_model, "api_key": cred.ai_key}
    if args.apply and not cfg["api_key"]:
        print("[错误] 后台模式 --apply 需要配置 LLM API key（credentials.json 的 ai.api_key）")
        return 1
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

    print(f"监听启动（后台AI/自动提交）：间隔 {interval}s，模式 {'apply（自动提交）' if args.apply else 'dry-run（只打草稿）'}，"
          f"模型 {cfg['model']}，已回复 {len(replied)} 条")
    if args.once:
        poll_once(args, cn_map, cfg, replied, set(), csv_writer, None)
        csv_file.close()
        print("（--once 单轮结束）")
        return 0

    try:
        while True:
            try:
                poll_once(args, cn_map, cfg, replied, set(), csv_writer, None)
                csv_file.flush()
            except Exception as e:
                print(f"  [轮次异常] {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止监听")
    finally:
        csv_file.close()
    return 0
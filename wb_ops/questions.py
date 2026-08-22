# -*- coding: utf-8 -*-
"""
wb_ops 买家提问查询与回复（原「获取和回复买家未处理提问」抓包）

查询各店未处理提问（isAnswered=false），回复需明确指定内容（人工 / AI 准备），
避免 AI 全自动误回复买家（公开发言，出错影响店铺）。
流程：GET questions 拉未处理列表（cursor 游标翻页）→ PATCH questions/answer 回复。
鉴权：WB cookie 会话三件套（wb_api.py）。
"""
import csv
import os
import time

from . import config
from . import credentials
from . import common
from . import mapping
from . import replicate
from . import wb_api

QUESTIONS = "https://seller-reviews.wildberries.ru/ns/fa-seller-api/reviews-ext-seller-portal/api/v2/questions"
ANSWER = "https://seller-reviews.wildberries.ru/ns/fa-seller-api/reviews-ext-seller-portal/api/v2/questions/answer"


def fetch_questions(session, limit=100):
    """GET 未处理提问（游标翻页到 hasMore=false）。返回全部问题列表。"""
    all_q = []
    cursor = ""
    while True:
        url = (f"{QUESTIONS}?cursor={cursor}&isAnswered=false"
               f"&limit={limit}&searchText=&sortOrder=dateDesc")
        d = wb_api.request(session, "GET", url)
        data = d.get("data") or {}
        qs = data.get("questions") or []
        all_q.extend(qs)
        if not data.get("hasMore") or not qs:
            break
        pages = data.get("pages") or {}
        cursor = pages.get("next") or pages.get("cursor") or pages.get("nextCursor") or ""
        if not cursor:
            break
    return all_q


def reply(session, question_id, text):
    """PATCH 回复指定提问。"""
    return wb_api.request(session, "PATCH", ANSWER,
                          json={"answerText": text, "questionId": question_id})


def process_shop(shop, root_version, args, rows, cn_map):
    st = {"unanswered": 0, "replied": 0, "failed": 0}
    s = wb_api.make_session(shop, root_version)
    name = shop.get("shopName", shop.get("shopId"))
    print(f"\n=== 店铺 {name} (id={shop['shopId']}) ===")
    questions = fetch_questions(s)
    st["unanswered"] = len(questions)
    if not questions:
        print("  无未处理提问")
        return st
    print(f"  [列表] {len(questions)} 条未处理提问")

    no_detail = getattr(args, "no_detail", False)
    for q in questions:
        qinfo = q.get("questionInfo") or {}
        pinfo = q.get("productInfo") or {}
        rtime = q.get("responseTime") or {}
        vc = pinfo.get("supplierArticle") or ""
        nm_id = pinfo.get("wbArticle")
        cn = cn_map.get(vc, {}).get("cn", "") if vc else ""
        # 关联商品信息：映射表中文名 + card.json（描述/特征/颜色）+ 价格映射表（店铺价CNY）
        title = pinfo.get("name", "")
        brand = colors = price = description = options = ""
        if not no_detail and nm_id:
            info = replicate.fetch_product_info(nm_id, vc=vc, own=cn_map)
            if info.get("title"):
                title = info["title"]
            brand = info.get("brand", "")
            colors = info.get("colors", "")
            price = info.get("price", "")
            description = info.get("description", "")
            options = info.get("options", "")
            time.sleep(0.3)
        rows.append({
            "店铺": name, "shopId": shop["shopId"],
            "提问ID": q.get("id"),
            "中文名": cn,
            "商品标题": title,
            "品牌": brand,
            "颜色": colors,
            "价格": price,
            "商品描述": description,
            "商品特征": options,
            "供应商编码": vc,
            "买家问题": qinfo.get("text", ""),
            "提问人": qinfo.get("userName", ""),
            "回复截止": rtime.get("deadlineDate", ""),
            "结果": "",
        })
        print(f"    id={q.get('id')} 【{cn}】[{title[:40]}] {qinfo.get('userName', '')}: {qinfo.get('text', '')[:50]}")

    if args.reply:
        target_ids = []
        if args.reply_all:
            if not args.yes:
                print(f"  ⚠ --reply-all 将回复本店全部 {len(questions)} 条提问（公开发言），加 --yes 确认")
                return st
            target_ids = [q.get("id") for q in questions if q.get("id")]
        elif args.question_id:
            target_ids = [args.question_id]
        for qid in target_ids:
            try:
                d = reply(s, qid, args.reply)
                ok = not d.get("error")
                res = "已回复" if ok else f"失败:{d.get('errorText') or 'error'}"
                if ok:
                    st["replied"] += 1
                else:
                    st["failed"] += 1
            except Exception as e:
                res = f"失败:{str(e)[:50]}"
                st["failed"] += 1
            for row in rows:
                if row["提问ID"] == qid:
                    row["结果"] = res
            print(f"    >> id={qid} -> {res}")
            time.sleep(0.5)
    return st


def run(args):
    common.ensure_utf8_stdout()
    if args.reply and not (args.question_id or args.reply_all):
        print("[错误] --reply 需配合 --question-id（回复单条）或 --reply-all（回复全部）")
        return 1
    if args.reply_all and not args.reply:
        print("[错误] --reply-all 需配合 --reply 指定回复内容")
        return 1

    cred = credentials.get()
    shops = cred.wb_shops()
    if not shops:
        print("[错误] credentials.json 中没有已填 cookie 的店铺")
        return 1
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if s["shopId"] in want]
        if not shops:
            print(f"[错误] 指定店铺 {args.shops} 未在凭证中找到")
            return 1

    root_version = cred.root_version
    shop_names = [f"{s['shopName']}({s['shopId']})" for s in shops]
    mode = "  [回复模式]" if args.reply else ""
    print(f"店铺 {len(shops)} 个: {shop_names}{mode}")

    cn_map = {}
    try:
        cn_map = mapping.load_mapping_state()[0]
    except Exception:
        pass

    rows = []
    totals = {"unanswered": 0, "replied": 0, "failed": 0}
    for shop in shops:
        try:
            st = process_shop(shop, root_version, args, rows, cn_map)
            for k in totals:
                totals[k] += st[k]
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {shop['shopName']}: {e}（该店中止，继续下一店）")
        except Exception as e:
            print(f"  [警告] 店铺 {shop['shopName']} 处理失败: {e}")
        time.sleep(0.5)

    print(f"\n[汇总] 未处理提问 {totals['unanswered']} | 已回复 {totals['replied']} | 失败 {totals['failed']}")
    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"买家提问_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "shopId", "提问ID", "中文名", "商品标题",
                                              "品牌", "颜色", "价格", "商品描述", "商品特征", "供应商编码",
                                              "买家问题", "提问人", "回复截止", "结果"])
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")
    return 0

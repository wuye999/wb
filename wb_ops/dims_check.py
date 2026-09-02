# -*- coding: utf-8 -*-
"""
wb_ops 查询 WB 尺寸/重量偏差待验证商品（dims-check）

WB 平台会标记包装尺寸/重量偏差的商品为需验证（“检查包装尺寸 / 请检查重量”）。
本模块用 tableListImprovable 拉取这类商品，绑定映射表中文名并按 --name 筛选，
供后续用 dimension --vc ... --dims ... 单独测试尺寸。
支持 --type：dims（尺寸偏差）/ weight（重量偏差）/ all（两者合并去重）。

鉴权：WB cookie 三件套会话（wb_api.make_session），只读，不写 BCS / 不移回收站。
"""
import csv
import os
import time
from datetime import datetime

from . import common
from . import config
from . import credentials
from . import mapping
from . import wb_api

WB_IMPROVABLE_LIST = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/tableListImprovable"
PAGE_SIZE = 20      # 每页条数（抓包 cursor.n=20）
MAX_PAGES = 50      # 翻页安全上限（防死循环）
SHOP_SLEEP = 0.5    # 店间间隔（串行）

# 各查询类型：filter 偏差字段 / 展示标签 / CSV 文件名前缀
TYPES = {
    "dims":   {"key": "withDimensionDeviation", "label": "尺寸偏差", "fname": "尺寸"},
    "weight": {"key": "hasWeightBruttoDeviation", "label": "重量偏差", "fname": "重量"},
}


def fetch_improvable(session, limit=0, key="withDimensionDeviation"):
    """分页拉取指定偏差类型的待验证卡片。limit>0 截断前 N 条。
    翻页终止：空 cards 或 cursor.next==false 或 MAX_PAGES；按 nmID 去重。"""
    cards, seen = [], set()
    cursor = {"n": PAGE_SIZE}
    for _ in range(MAX_PAGES):
        body = {
            "sort": [{"columnID": 11, "order": "desc"}],
            "filter": {"search": "", "paidOptions": {}, key: True},
            "cursor": cursor,
        }
        d = wb_api.request_post(session, WB_IMPROVABLE_LIST, body, allow_400_json=True)
        if d.get("error"):
            raise RuntimeError(f"查询偏差列表失败: {d.get('errorText') or d.get('error')}")
        data = d.get("data") or {}
        page = data.get("cards") or []
        if not page:
            break
        for c in page:
            nid = c.get("nmID")
            if nid is None or nid in seen:
                continue
            seen.add(nid)
            cards.append(c)
        if limit > 0 and len(cards) >= limit:
            return cards[:limit]
        cur = data.get("cursor") or {}
        if not cur.get("next"):
            break
        cursor = {"n": PAGE_SIZE}
        if cur.get("value") is not None:
            cursor["value"] = cur["value"]
        if cur.get("nmID"):
            cursor["nmID"] = cur["nmID"]
    return cards[:limit] if limit > 0 else cards


def _reasons(c):
    """合并返回一条展示文案：flaws(中文字符串数组) + flaws(dict数组) + comments(详情)，‘；’连接。"""
    parts, seen = [], set()

    def push(s):
        s = str(s or "").strip()
        if s and s not in seen:
            seen.add(s)
            parts.append(s)

    eb = c.get("externalBan")
    if isinstance(eb, dict):
        for k in ("reasonRu", "reason", "text"):
            push(eb.get(k))
    flaws = c.get("flaws")
    if isinstance(flaws, list):
        for f in flaws[:4]:
            if isinstance(f, str):
                push(f)
            elif isinstance(f, dict):
                push(f.get("reasonRu") or f.get("title") or f.get("text"))
    cm = c.get("comments")
    if isinstance(cm, list) and cm:
        for x in cm[:2]:
            if isinstance(x, str):
                push(x)
            elif isinstance(x, dict):
                push(x.get("text") or x.get("reasonRu"))
    return "；".join(parts)


def run(args):
    common.ensure_utf8_stdout()
    try:
        state, _ = mapping.load_mapping_state()
    except Exception:
        state = {}
    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已填 cookie 的店铺")
        return 1
    pairs = [(s, s["shopId"]) for s in wb_shops]
    if getattr(args, "shops", ""):
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        pairs = [p for p in pairs if p[1] in want]
    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）")
        return 1
    name_kw = getattr(args, "name", "") or ""
    limit = getattr(args, "limit", 0)
    typ = getattr(args, "type", "dims") or "dims"
    if typ not in ("dims", "weight", "all"):
        print(f"[错误] --type 非法：{typ}（应为 dims/weight/all，其一）")
        return 1
    names = ", ".join(f"{s['shopName']}({s['shopId']})" for s, _ in pairs)
    lbl = "尺寸+重量" if typ == "all" else TYPES[typ]["label"]
    kw = f"｜中文名含「{name_kw}」" if name_kw else "｜全部"
    print(f"店铺 {len(pairs)} 个: {names}（只读；类型={lbl}{kw}）")

    rows = []
    total = 0
    for shop, sid in pairs:
        nm = shop["shopName"]
        by_nid = {}
        try:
            wb_s = wb_api.make_session(shop, cred.root_version)
            types = [typ] if typ != "all" else ["dims", "weight"]
            for t in types:
                cfg = TYPES[t]
                cards = fetch_improvable(wb_s, limit, key=cfg["key"])
                print(f"\n=== {nm}({sid}) {cfg['label']} {len(cards)} 个 ===")
                for c in cards:
                    nid = c.get("nmID")
                    if nid not in by_nid:
                        by_nid[nid] = {"card": c, "kinds": []}
                    if cfg["label"] not in by_nid[nid]["kinds"]:
                        by_nid[nid]["kinds"].append(cfg["label"])
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {nm}: {e}")
            continue
        except Exception as e:
            print(f"  [警告] 店铺 {nm} 查询失败: {e}")
            continue
        # 重查每张卡：同一卡片对 dims/weight 可能返回不同原因，取最后一张并合并原因
        for nid, rec in by_nid.items():
            c = rec["card"]
            vc = c.get("vendorCode") or ""
            sv = state.get(vc)
            cn = sv.get("cn") if isinstance(sv, dict) else ""
            if name_kw and name_kw not in cn:
                continue
            kinds = "+".join(rec["kinds"])
            reason = _reasons(c)
            rows.append({"店铺": nm, "ID": nid, "偏差类型": kinds, "vendorCode": vc,
                         "中文名": cn, "标题": c.get("title") or "", "原因": reason})
            print(f"  [{kinds}] {vc or nid} | {cn or c.get('title') or ''}"
                  f"{' | ' + reason[:50] if reason else ''}")
            total += 1
        time.sleep(SHOP_SLEEP)

    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = "尺寸" if typ == "dims" else ("重量" if typ == "weight" else "尺寸重量")
        path = os.path.join(config.LOG_DIR, f"{fname}偏差待验证_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "ID", "偏差类型", "vendorCode", "中文名", "标题", "原因"])
            w.writeheader()
            w.writerows(rows)
        print(f"\n[日志] 匹配 {total} 条 → {path}")
        print("\n[提示] 拿上表 vendorCode 逐个测试尺寸：python wb.py dimension --vc <vc[,vc...]> --dims \"长*宽*高/毛重\" --apply")
    else:
        print("\n没有匹配的待验证商品")
    return 0
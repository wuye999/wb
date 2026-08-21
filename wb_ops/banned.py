# -*- coding: utf-8 -*-
"""
wb_ops 查询并删除被阻止的商品（banned）

WB 平台会把违规/有问题的商品标记为「被阻止」（banned，flaws 含"被阻止"），
本模块：查询被阻止商品列表（tableListImprovable 分页）→ dry-run 预览 → --apply
移到回收站（moveNmsToTrash，可恢复）→ 自动复核（count before/after + 重查列表）。

鉴权：WB cookie 三件套会话（wb_api.make_session），不需要 BCS。
"""
import csv
import os
import time
from datetime import datetime

from . import common
from . import config
from . import credentials
from . import wb_api

WB_BANNED_LIST = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/tableListImprovable"
WB_MOVE_TRASH = "https://seller-content.wildberries.ru/ns/source/content-card/source/v2/moveNmsToTrash"
WB_BANNED_COUNT = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/count"

PAGE_SIZE = 20      # 列表每页条数（抓包 cursor.n=20）
MAX_PAGES = 50      # 翻页安全上限（防死循环）
TRASH_CHUNK = 300   # 移回收站批量 ≤300/批（项目硬性规则）
SHOP_SLEEP = 0.5    # 店间间隔（串行，勿并行）
CHUNK_SLEEP = 0.3   # 批间间隔


def fetch_banned_cards(session, limit=0):
    """分页拉取全部被阻止卡片。limit>0 截断前 N 条。
    返回 cards 列表（每项含 nmID/vendorCode/title/externalBan 等）。
    翻页终止：空 cards 或 cursor.next==false 或 MAX_PAGES；按 nmID 去重防游标重复。"""
    cards, seen = [], set()
    cursor = {"n": PAGE_SIZE}
    for _ in range(MAX_PAGES):
        body = {
            "sort": [{"columnID": 11, "order": "desc"}],
            "filter": {"search": "", "paidOptions": {}, "banned": True},
            "cursor": cursor,
        }
        d = wb_api.request_post(session, WB_BANNED_LIST, body, allow_400_json=True)
        if d.get("error"):
            raise RuntimeError(f"查询被阻止列表失败: {d.get('errorText') or d.get('error')}")
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
        # 翻页：带响应游标（value/nmID），n 保持页大小
        cursor = {"n": PAGE_SIZE}
        if cur.get("value") is not None:
            cursor["value"] = cur["value"]
        if cur.get("nmID"):
            cursor["nmID"] = cur["nmID"]
    return cards[:limit] if limit > 0 else cards


def move_to_trash(session, nm_ids):
    """批量移到回收站（裸数组 body）。返回 (moved, failed{nmID: 原因})。
    响应无成功清单：error=false + 空 additionalErrors 即全部成功；失败解析对齐 clean.delete_nms。"""
    moved, failed = [], {}
    for i in range(0, len(nm_ids), TRASH_CHUNK):
        chunk = nm_ids[i:i + TRASH_CHUNK]
        body = [{"nmID": nid} for nid in chunk]
        try:
            d = wb_api.request_post(session, WB_MOVE_TRASH, body, allow_400_json=True)
        except Exception as e:
            for nid in chunk:
                failed[nid] = str(e)[:60]
            time.sleep(CHUNK_SLEEP)
            continue
        if d.get("error"):
            for nid in chunk:
                failed[nid] = d.get("errorText") or "未知原因"
        else:
            add = d.get("additionalErrors") or {}
            if add:
                for k, v in add.items():
                    if k == "errors":
                        continue
                    try:
                        failed[int(k)] = v
                    except (ValueError, TypeError):
                        pass
                for e in add.get("errors") or []:
                    try:
                        nid = (e.get("params") or {}).get("1")
                        if nid:
                            failed[int(nid)] = e.get("trKey", "")
                    except (ValueError, TypeError):
                        pass
                for nid in chunk:
                    if nid not in failed:
                        failed[nid] = "未知原因"
        for nid in chunk:
            if nid not in failed:
                moved.append(nid)
        time.sleep(CHUNK_SLEEP)
    return moved, failed


def count_banned(session):
    """GET 商品状态计数。返回 data dict（bannedCard / processedCard / basketCard 等）。"""
    d = wb_api.request(session, "GET", WB_BANNED_COUNT)
    return d.get("data") or {}


def _card_cn(vc):
    """（可选）映射表中文名：banned 模块不强制依赖，仅用于展示增强。"""
    try:
        from . import mapping
        state, _ = mapping.load_mapping_state()
        if vc and vc in state:
            return state[vc].get("cn") or ""
    except Exception:
        pass
    return ""


def process_shop(shop, sid, wb_session, args, rows):
    """单店处理：查询 → dry-run 预览 / --apply 移回收站 + 自动复核。
    返回 stats dict（total/moved/failed）。"""
    name = shop["shopName"]
    st = {"total": 0, "moved": 0, "failed": 0}
    print(f"\n=== 店铺 {name}({sid}) 被阻止商品 ===")
    cards = fetch_banned_cards(wb_session, args.limit)
    if not cards:
        print("  没有被阻止商品")
        return st
    st["total"] = len(cards)
    print(f"  [列表] 被阻止 {len(cards)} 个")

    if not args.apply:
        for c in cards:
            vc = c.get("vendorCode") or ""
            cn = _card_cn(vc)
            reason = ((c.get("externalBan") or {}).get("reasonRu")
                      or ((c.get("externalBan") or {}).get("reason") or "")
                      or (c.get("comments") or [""])[0] or "")
            rows.append({"店铺": name, "ID": c.get("nmID"), "类型": "nmId",
                         "vendorCode": vc, "中文名": cn, "标题": c.get("title") or "",
                         "阻止原因": reason, "结果": "DRY将移回收站"})
            print(f"    {vc or c.get('nmID')} | {cn or c.get('title') or ''}"
                  f"{' | ' + reason[:40] if reason else ''}")
        return st

    # ---- apply ----
    before = count_banned(wb_session).get("bannedCard", 0)
    nm_ids = [c["nmID"] for c in cards]
    moved, failed = move_to_trash(wb_session, nm_ids)
    st["moved"] = len(moved)
    st["failed"] = len(failed)
    for c in cards:
        nid = c.get("nmID")
        vc = c.get("vendorCode") or ""
        cn = _card_cn(vc)
        reason = ((c.get("externalBan") or {}).get("reasonRu")
                  or ((c.get("externalBan") or {}).get("reason") or "")
                  or (c.get("comments") or [""])[0] or "")
        res = "已移回收站" if nid in moved else f"失败:{str(failed.get(nid, '未知'))[:40]}"
        rows.append({"店铺": name, "ID": nid, "类型": "nmId",
                     "vendorCode": vc, "中文名": cn, "标题": c.get("title") or "",
                     "阻止原因": reason, "结果": res})
    for nid, why in failed.items():
        print(f"    [失败] nmID={nid}: {why}")
    if failed:
        print("  [提示] 部分商品移回收站失败：若为有库存/在途原因，需先归零库存后再重跑")

    # 自动复核（WB 侧 count + 重查列表，快且不依赖 BCS）
    if not args.no_verify and moved:
        try:
            after = count_banned(wb_session).get("bannedCard", 0)
            remain = [c for c in fetch_banned_cards(wb_session)
                      if c.get("nmID") in set(moved)]
            print(f"  [验证] bannedCard {before} → {after}；已提交 {len(moved)} 个，"
                  f"仍留在被阻止列表 {len(remain)} 个")
            if remain:
                print("  [验证] 有商品仍在被阻止列表：WB 延迟或部分失败，可稍后重跑 wb.py banned 复核")
        except Exception as e:
            print(f"  [验证] 失败：{e}（可稍后手动 wb.py fetch 复核）")
    return st


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已填 cookie 的店铺")
        return 1
    pairs = [(s, s["shopId"]) for s in wb_shops]
    if args.shops:
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        pairs = [p for p in pairs if p[1] in want]
    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）")
        return 1
    pair_names = [f"{s['shopName']}({s['shopId']})" for s, _ in pairs]
    print(f"店铺 {len(pairs)} 个: {pair_names}{'' if args.apply else '  [dry-run]'}")

    if args.apply:
        # 不可逆确认（移到回收站后可恢复，但批量操作仍按项目惯例确认）
        from . import ops
        if not args.yes:
            ok = ops.confirm_irreversible("将被阻止商品移到回收站", len(pairs), args.yes)
            if not ok:
                print("已取消")
                return 0

    rows = []
    totals = {"total": 0, "moved": 0, "failed": 0}
    for shop, sid in pairs:
        try:
            wb_s = wb_api.make_session(shop, cred.root_version)
            st = process_shop(shop, sid, wb_s, args, rows)
            for k in totals:
                totals[k] += st[k]
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {shop['shopName']}: {e}（该店中止，继续下一店）")
        except Exception as e:
            print(f"  [警告] 店铺 {shop['shopName']} 处理失败: {e}")
        time.sleep(SHOP_SLEEP)

    print(f"\n[汇总] 被阻止 {totals['total']} | "
          f"{'已移回收站 ' + str(totals['moved']) if args.apply else '预览 ' + str(len(rows))}"
          f" | 失败 {totals['failed']}")
    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"被阻止商品_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "ID", "类型", "vendorCode",
                                              "中文名", "标题", "阻止原因", "结果"])
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")

    if not args.apply:
        print("\n（dry-run 未执行任何删除；确认清单后加 --apply 执行）")
    return 0

"""
sellerops 查询并删除被阻止的商品（banned）

WB 平台会把违规/有问题的商品标记为「被阻止」（banned，flaws 含"被阻止"），
本模块：查询被阻止商品列表（tableListImprovable 分页）→ dry-run 预览 → --apply
移到回收站（moveNmsToTrash，可恢复），记录提交结果后结束。

鉴权：WB cookie 三件套会话（wb_api.make_session），不需要 BCS。
"""
import csv
import os
import time
from datetime import datetime
from functools import lru_cache
from sellerops.features.products.trash import move_in_session
from sellerops.persistence.mappings import MappingRepository

from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.integrations.wildberries import client as wb_api

WB_BANNED_LIST = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/tableListImprovable"

PAGE_SIZE = 20      # 列表每页条数（抓包 cursor.n=20）
MAX_PAGES = 50      # 翻页安全上限（防死循环）
SHOP_SLEEP = 0.5    # 店间间隔（串行，勿并行）


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
        d = wb_api.request_post(session, WB_BANNED_LIST, body, read_only=True, allow_400_json=True)
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


@lru_cache(maxsize=1)
def _names(data: config.DataPaths):
    resolve, _ = MappingRepository.build_vc_resolver()
    return resolve


def _card_cn(vc):
    if not vc:
        return ""
    try:
        return _names(config.current)(vc, "")
    except (OSError, ValueError):
        return ""


def process_shop(shop, sid, wb_session, args, rows):
    """单店处理：查询 → dry-run 预览 / --apply 提交移回收站，提交后结束。
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

    nm_ids = [c["nmID"] for c in cards]
    result = move_in_session(wb_session, nm_ids)
    moved = {row["nmID"] for row in result["details"] if row["ok"]}
    failed = {row["nmID"]: row["msg"] for row in result["details"] if not row["ok"]}
    st["moved"] = len(moved)
    st["failed"] = len(failed)
    for c in cards:
        nid = c.get("nmID")
        vc = c.get("vendorCode") or ""
        cn = _card_cn(vc)
        reason = ((c.get("externalBan") or {}).get("reasonRu")
                  or ((c.get("externalBan") or {}).get("reason") or "")
                  or (c.get("comments") or [""])[0] or "")
        res = "已提交" if nid in moved else f"失败:{str(failed.get(nid, '未知'))[:40]}"
        rows.append({"店铺": name, "ID": nid, "类型": "nmId",
                     "vendorCode": vc, "中文名": cn, "标题": c.get("title") or "",
                     "阻止原因": reason, "结果": res})
    for nid, why in failed.items():
        print(f"    [失败] nmID={nid}: {why}")
    if failed:
        print("  [提示] 部分商品移回收站失败：若为有库存/在途原因，需先归零库存后再重跑")

    common.print_write_hint()
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
        from sellerops.features.products import operations as ops
        if not args.yes:
            ok = ops.confirm_irreversible("trash", len(pairs), args.yes)
            if not ok:
                print("已取消")
                return 0

    rows = []
    totals = {"total": 0, "moved": 0, "failed": 0}
    shop_errors = 0
    for shop, sid in pairs:
        try:
            wb_s = wb_api.make_session(shop, cred.root_version)
            st = process_shop(shop, sid, wb_s, args, rows)
            for k in totals:
                totals[k] += st[k]
        except common.CookieExpiredError as e:
            shop_errors += 1
            print(f"  [警告] 店铺 {shop['shopName']}: {e}（该店中止，继续下一店）")
        except Exception as e:
            shop_errors += 1
            print(f"  [警告] 店铺 {shop['shopName']} 处理失败: {e}")
        time.sleep(SHOP_SLEEP)

    print(f"\n[汇总] 被阻止 {totals['total']} | "
          f"{'下架已受理 ' + str(totals['moved']) if args.apply else '预览 ' + str(len(rows))}"
          f" | 失败 {totals['failed']} | 店铺异常 {shop_errors}")
    if rows:
        os.makedirs(config.current.log_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.current.log_dir, f"被阻止商品_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["店铺", "ID", "类型", "vendorCode",
                                              "中文名", "标题", "阻止原因", "结果"])
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {path}")

    if not args.apply:
        print("\n（dry-run 未执行任何删除；确认清单后加 --apply 执行）")
    return 1 if totals["failed"] or shop_errors else 0

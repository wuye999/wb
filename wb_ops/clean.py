# -*- coding: utf-8 -*-
"""
wb_ops 回收站 / 草稿箱商品清理（原 wb_clean_delete.py）

回收站：BCS 拉 TRASH → 有库存商品先归零 → WB deleteAllSize 一键清空
草稿箱：BCS 拉 ERROR → WB deleteByNMUUID 按 UUID 删除
鉴权：BCS（bcs.py）+ WB（wb_api.py 会话三件套）
"""
import csv
import os
import time

from . import bcs
from . import config
from . import credentials
from . import common
from . import wb_api
WB_DELETE_NMS = "https://seller-content.wildberries.ru/ns/source/content-card/source/deleteNMsByIDs"
WB_DELETE_UUID = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/errorCard/deleteByNMUUID"
WB_DELETE_ALL = "https://seller-content.wildberries.ru/ns/source/content-card/source/deleteAllSize"
WB_ERROR_LIST = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/errorsTableListV5"
DEL_CHUNK = 50
STOCK_CHUNK = 200
ERROR_PAGE = 200      # errorsTableListV5 每页条数（游标可续，抓包用 20，取较大值降请求次数）
ERROR_MAX_PAGES = 50  # 游标分页上限保护


def _stock_url():
    return bcs.base_url() + "/shopKeeper/stock/batchSetByChrtIdsBatch"


# ---------- 回收站：删除 + 失败归零 ----------
def delete_nms(session, nm_ids):
    deleted, failed = [], {}
    for i in range(0, len(nm_ids), DEL_CHUNK):
        chunk = nm_ids[i:i + DEL_CHUNK]
        d = wb_api.request_post(session, WB_DELETE_NMS, {"nmIDs": chunk}, allow_400_json=True)
        data = d.get("data") or {}
        deleted.extend(data.get("nmIDs") or [])
        add = d.get("additionalErrors") or {}
        for k, v in add.items():
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
            if nid not in deleted and nid not in failed:
                failed[nid] = d.get("errorText") or "未知原因"
        time.sleep(0.3)
    return deleted, failed


def delete_all_size(session):
    """一键清空回收站（无 body）。返回完整响应 dict（error/errorText/data）。
    有库存商品删不掉时返回 HTTP 400 + error:true（StockCount>0），按正常返回处理不抛错。"""
    return wb_api.request_post(session, WB_DELETE_ALL, None, allow_400_json=True)


def set_stock_zero(shop_id, row_map, nm_ids):
    results = {}
    # 归零一律用默认仓库（莫斯科）；成都仓库不操作
    default_wh = bcs.default_warehouse_id(shop_id)
    if default_wh is None:
        for nid in nm_ids:
            results[nid] = "err:无默认仓库"
        return results
    groups = {default_wh: []}
    for nid in nm_ids:
        row = row_map.get(nid)
        if not row:
            results[nid] = "err:无BCS行"
            continue
        sl = row.get("sizeList") or []
        if not sl:
            results[nid] = "err:无sizeList"
            continue
        chrt = sl[0].get("chrtId")
        if not chrt:
            results[nid] = "err:无chrtId"
            continue
        groups[default_wh].append((nid, chrt))

    for wh_id, items in groups.items():
        for i in range(0, len(items), STOCK_CHUNK):
            chunk = items[i:i + STOCK_CHUNK]
            body = {"shopId": shop_id, "warehouses": [
                {"warehouseId": wh_id,
                 "stockItems": [{"chrtId": c, "amount": 0} for _, c in chunk]}]}
            try:
                r = bcs.http_post_json(_stock_url(), body)
                ok = r.get("code") == 200
                for nid, _ in chunk:
                    results[nid] = "已归零" if ok else f"归零失败:{r.get('msg')}"
            except Exception as e:
                for nid, _ in chunk:
                    results[nid] = f"归零失败:{str(e)[:40]}"
            time.sleep(0.2)
    return results


# ---------- 草稿箱：查询 + 删除 ----------
def fetch_error_cards(session):
    """WB 草稿箱错误卡片列表（errorsTableListV5，游标分页）。返回 list（每条含 vendorCodes/nmUUIDs/updateAt）。
    ★ 权威列表来源：WB 原生接口提供真实 nmUUIDs，替代 BCS ERROR 快照（后者常缺/错 uuid，导致删除落空）。"""
    cursor = {"n": ERROR_PAGE}
    cards = []
    for _ in range(ERROR_MAX_PAGES):
        body = {"sort": {"searchValue": "", "sortColumn": "updateAt", "ascending": False},
                "cursor": cursor}
        d = wb_api.request_post(session, WB_ERROR_LIST, body, allow_400_json=True)
        data = d.get("data") or {}
        cards.extend(data.get("items") or [])
        cur = data.get("cursor") or {}
        if not cur.get("next"):
            break
        cursor = {"n": ERROR_PAGE, "updatedAt": cur.get("updatedAt"),
                  "batchUID": cur.get("batchUID")}
        time.sleep(0.3)
    return cards


def delete_nm_uuids(session, uuids):
    """按 nmUUID 批量删除草稿（分块调用 deleteByNMUUID）。返回 (ok_n, fails)。
    fails: [(uuid, reason)]。成功则以响应 error==false 判定。"""
    ok_n, fails = 0, []
    for i in range(0, len(uuids), DEL_CHUNK):
        chunk = uuids[i:i + DEL_CHUNK]
        d = wb_api.request_post(session, WB_DELETE_UUID, {"nmUUIDs": chunk}, allow_400_json=True)
        if d.get("error"):
            fails.extend((u, d.get("errorText") or "删除失败") for u in chunk)
        else:
            ok_n += len(chunk)
        time.sleep(0.3)
    return ok_n, fails


# ---------- 主流程 ----------
def process_basket(shop, sid, wb_session, args, rows):
    name = shop["shopName"]
    st = {"total": 0, "deleted": 0, "failed": 0, "zeroed": 0}
    print(f"\n=== 店铺 {name}({sid}) 回收站 ===")
    trash = bcs.fetch_shop_products(sid, "TRASH")
    if not trash:
        print("  回收站为空")
        return st
    st["total"] = len(trash)
    row_map = {r.get("nmId"): r for r in trash if r.get("nmId")}
    # 识别有库存商品（有库存/在途的删不掉，先归零）
    stocked = []
    for r in trash:
        nid = r.get("nmId")
        if not nid:
            continue
        has_stock = any((stk.get("amount") or 0) > 0
                        for sz in (r.get("sizeList") or [])
                        for stk in (sz.get("stockList") or []))
        if has_stock:
            stocked.append(nid)
    if args.limit > 0:
        stocked = stocked[: args.limit]
    print(f"  [列表] 回收站 {len(trash)} 条，其中有库存 {len(stocked)} 个将先归零")

    if not args.apply:
        for r in trash:
            rows.append({"店铺": name, "ID": r.get("nmId"), "类型": "nmId",
                         "vendorCode": r.get("vendorCode"), "结果": "DRY将一键清空"})
        print("  [dry-run] 未执行：将一键清空整个回收站（deleteAllSize，不可逆）")
        return st

    # apply：① 先归零有库存商品；② 一键清空
    if stocked:
        print(f"  [归零] 对 {len(stocked)} 个有库存商品设置库存 0 ...")
        zero_res = set_stock_zero(sid, row_map, stocked)
        for nid, res in zero_res.items():
            st["zeroed"] += 1 if res == "已归零" else 0
            print(f"    nmID={nid} -> {res}")
    print(f"  [一键清空] deleteAllSize ...")
    resp = delete_all_size(wb_session)
    data = resp.get("data") or {}
    cleared_nm = data.get("nmIDs") or []
    stock_count = data.get("StockCount", 0)
    st["deleted"] = len(cleared_nm)
    for r in trash:
        nid = r.get("nmId")
        if nid in cleared_nm:
            res = "已清空"
        elif resp.get("error"):
            res = "清空失败(有库存/在途)"
        else:
            res = "未清空"
        rows.append({"店铺": name, "ID": nid, "类型": "nmId",
                     "vendorCode": r.get("vendorCode"), "结果": res})
    if resp.get("error"):
        print(f"  [结果] 清空失败：{resp.get('errorText')}"
              f"（已清空 {len(cleared_nm)} 个，仍有 {stock_count} 个有库存未清空）")
    else:
        print(f"  [结果] 已清空 {len(cleared_nm)} 个")
    return st


def process_draft(shop, sid, wb_session, args, rows):
    name = shop["shopName"]
    st = {"total": 0, "deleted": 0, "failed": 0, "zeroed": 0}
    print(f"\n=== 店铺 {name}({sid}) 草稿箱 ===")
    cards = fetch_error_cards(wb_session)
    # 摊平：vendorCodes[i] ↔ nmUUIDs[i] 依序配对，vcs 索引对不齐则填空
    entries, seen_uuid = [], set()
    for c in cards:
        vcs = c.get("vendorCodes") or []
        us = c.get("nmUUIDs") or []
        for i, u in enumerate(us):
            if not u or u in seen_uuid:
                continue
            seen_uuid.add(u)
            vc = vcs[i] if i < len(vcs) else ""
            entries.append({"vc": vc or "", "uuid": u, "updateAt": c.get("updateAt") or ""})
    if not entries:
        print("  草稿箱为空")
        return st
    st["total"] = len(entries)
    if args.limit > 0:
        entries = entries[: args.limit]
    uuids = [e["uuid"] for e in entries]
    print(f"  [列表] 草稿箱 {len(cards)} 条卡片，本次处理 {len(uuids)} 个")

    if not args.apply:
        for e in entries:
            rows.append({"店铺": name, "ID": e["uuid"], "类型": "nmUuid",
                         "vendorCode": e["vc"], "结果": "DRY将删"})
        print("  [dry-run] 未执行删除（共 %d 条将删）" % len(uuids))
        return st

    ok_n, fails = delete_nm_uuids(wb_session, uuids)
    st["deleted"] = ok_n
    st["failed"] = len(fails)
    print(f"  [删除] 成功 {ok_n}，失败 {len(fails)}")
    fail_reason = {u: str(cd) for u, cd in fails}
    for e in entries:
        res = "已删除" if e["uuid"] not in fail_reason else f"失败:{fail_reason[e['uuid']][:40]}"
        rows.append({"店铺": name, "ID": e["uuid"], "类型": "nmUuid",
                     "vendorCode": e["vc"], "结果": res})
    return st


def run(args):
    common.ensure_utf8_stdout()
    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已填 cookie 的店铺")
        return 1
    targets = ["draft", "basket"] if args.target == "all" else [args.target]
    mode_names = {"draft": "草稿箱", "basket": "回收站"}
    wants = {int(x) for x in args.shops.split(",") if x.strip()} if args.shops else None

    # 店铺配对：纯 draft 走 WB 原生列表（无需 BCS 枚举/同步，BCS 凭证失效也能跑）；
    # 含 basket 时仍需 BCS（回收站 deleteAllSize 依赖 BCS TRASH）。
    if targets == ["draft"]:
        pairs = [(s, s.get("shopId")) for s in wb_shops if s.get("shopId")]
        if wants:
            pairs = [p for p in pairs if p[1] in wants]
    else:
        bcs_shops = {s["id"]: s["name"] for s in bcs.fetch_shop_list()}
        pairs = [(s, s["shopId"]) for s in wb_shops if s["shopId"] in bcs_shops]
        if wants:
            pairs = [p for p in pairs if p[1] in wants]
        if not args.no_sync and args.apply:
            print("\n[同步] 刷新 BCS 缓存（约 40s）...")
            bcs.sync_shops_parallel([p[1] for p in pairs])
    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）")
        return 1
    root_version = cred.root_version
    pair_names = [f"{s['shopName']}({s['shopId']})" for s, _ in pairs]
    print(f"店铺 {len(pairs)} 个: {pair_names}"
          f" | 目标: {' → '.join(mode_names[t] for t in targets)}"
          f"{'' if args.apply else '  [dry-run]'}")

    for tgt in targets:
        mode = mode_names[tgt]
        rows = []
        totals = {"total": 0, "deleted": 0, "failed": 0, "zeroed": 0}
        print(f"\n########## 清理{mode} ##########")
        for shop, sid in pairs:
            try:
                wb_s = wb_api.make_session(shop, root_version)
                st = process_basket(shop, sid, wb_s, args, rows) if tgt == "basket" \
                    else process_draft(shop, sid, wb_s, args, rows)
                for k in totals:
                    totals[k] += st[k]
            except common.CookieExpiredError as e:
                print(f"  [警告] 店铺 {shop['shopName']}: {e}（该店中止，继续下一店）")
            except Exception as e:
                print(f"  [警告] 店铺 {shop['shopName']} 处理失败: {e}")
            time.sleep(0.5)

        print(f"\n[汇总-{mode}] 共 {totals['total']} | "
              f"{'已删除 ' + str(totals['deleted']) if args.apply else '预览 ' + str(len(rows))}"
              f" | 失败 {totals['failed']}" + (f" | 已归零 {totals['zeroed']}" if tgt == "basket" else ""))
        if rows:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(config.LOG_DIR, f"{mode}清理_{ts}.csv")
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["店铺", "ID", "类型", "vendorCode", "结果"])
                w.writeheader()
                w.writerows(rows)
            print(f"[日志] {path}")

    if not args.apply:
        print("\n（dry-run 未执行任何删除；确认清单后加 --apply 执行）")
    return 0

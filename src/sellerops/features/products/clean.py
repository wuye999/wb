"""按店铺和已选商品清理草稿及回收站，保存逐项受理结果。"""
from collections import Counter
from sellerops.domain.snapshot_values import positive_id, stock_at, valid_sizes
from sellerops.core.errors import AuthenticationError
import csv
import os
import time

from sellerops.integrations.bcs.client import client as bcs
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.core import utils as common
from sellerops.integrations.wildberries import client as wb_api
from sellerops.features.products import warehouse
WB_DELETE_NMS = "https://seller-content.wildberries.ru/ns/source/content-card/source/deleteNMsByIDs"
WB_DELETE_UUID = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/errorCard/deleteByNMUUID"
WB_ERROR_LIST = "https://seller-content.wildberries.ru/ns/viewer/content-card/viewer/errorsTableListV5"
DEL_CHUNK = 50
STOCK_CHUNK = 200
ERROR_PAGE = 200      # errorsTableListV5 每页条数（游标可续，抓包用 20，取较大值降请求次数）
ERROR_MAX_PAGES = 50  # 游标分页上限保护


# ---------- 回收站：删除 + 失败归零 ----------
def delete_nms(session, nm_ids):
    """按卡确认回执。缺失、重复或矛盾的 ID 不能计作受理。"""
    response = wb_api.request_post(session, WB_DELETE_NMS, {"nmIDs": nm_ids}, allow_400_json=True)
    unknown = dict.fromkeys(nm_ids, "结果未知：删除回执不完整或矛盾")
    if not isinstance(response, dict) or type(response.get("error")) is not bool:
        return unknown
    data = response.get("data") or {}
    errors = response.get("additionalErrors") or {}
    if not isinstance(data, dict) or not isinstance(data.get("nmIDs"), list) or not isinstance(errors, dict):
        return unknown
    counts = Counter(positive_id(value) for value in data["nmIDs"])
    if not counts.keys() <= set(nm_ids):
        return unknown
    failed = set()
    for key, details in errors.items():
        if key == "errors" and isinstance(details, list):
            for detail in details:
                params = detail.get("params") if isinstance(detail, dict) else None
                failed.add(positive_id(params.get("1")) if isinstance(params, dict) else None)
        else:
            failed.add(positive_id(key))
    if not failed <= set(nm_ids) or (response["error"] and not failed):
        return unknown
    return {nm: ("删除未受理" if nm in failed and not counts[nm] else
                 "删除已受理" if nm not in failed and counts[nm] == 1 else unknown[nm])
            for nm in nm_ids}


# ---------- 草稿箱：查询 + 删除 ----------
def fetch_error_cards(session):
    """WB 草稿箱错误卡片列表（errorsTableListV5，游标分页）。返回 list（每条含 vendorCodes/nmUUIDs/updateAt）。
    ★ 权威列表来源：WB 原生接口提供真实 nmUUIDs，替代 BCS ERROR 快照（后者常缺/错 uuid，导致删除落空）。"""
    cursor = {"n": ERROR_PAGE}
    cards = []
    seen = set()
    for _ in range(ERROR_MAX_PAGES):
        body = {"sort": {"searchValue": "", "sortColumn": "updateAt", "ascending": False},
                "cursor": cursor}
        d = wb_api.request_post(session, WB_ERROR_LIST, body, read_only=True, allow_400_json=True)
        data = d.get("data") or {}
        if d.get("error") or not isinstance(data.get("items"), list):
            raise ValueError("草稿清单响应不完整")
        cards.extend(data.get("items") or [])
        cur = data.get("cursor") or {}
        if not cur.get("next"):
            break
        cursor = {"n": ERROR_PAGE, "updatedAt": cur.get("updatedAt"),
                  "batchUID": cur.get("batchUID")}
        marker = (cursor["updatedAt"], cursor["batchUID"])
        if not all(marker) or marker in seen:
            raise ValueError("草稿清单游标缺失或重复")
        seen.add(marker)
        time.sleep(0.3)
    else:
        raise ValueError("草稿清单超过分页上限")
    return cards


def delete_nm_uuids(session, uuids):
    outcomes = {}
    for start in range(0, len(uuids), DEL_CHUNK):
        batch = uuids[start:start + DEL_CHUNK]
        try:
            reply = wb_api.request_post(session, WB_DELETE_UUID, {"nmUUIDs": batch}, allow_400_json=True)
            if reply.get("error") is False and not reply.get("additionalErrors"):
                status = "删除已受理"
            elif reply.get("error") is True:
                status = "删除未受理：" + str(reply.get("errorText") or "平台拒绝")
            else:
                status = "删除结果未知：回执不完整"
        except AuthenticationError:
            outcomes.update(dict.fromkeys(uuids[start:], "鉴权失败，本店已停止"))
            break
        except Exception as error:
            status = f"删除结果未知（{type(error).__name__}）"
        outcomes.update(dict.fromkeys(batch, status))
        if start + len(batch) < len(uuids):
            time.sleep(0.3)
    return outcomes


# ---------- 主流程 ----------
def process_basket(shop, sid, wb_session, args, rows, *, limit=None):
    name = shop["shopName"]
    st = {"total": 0, "deleted": 0, "failed": 0, "zeroed": 0}
    print(f"\n=== 店铺 {name}({sid}) 回收站 ===")
    wh = warehouse.resolve_warehouse(sid)
    if wh is None:
        raise ValueError("无法确认本店莫斯科仓")
    trash = bcs.fetch_shop_products(sid, "TRASH")
    limit = args.limit if limit is None else limit
    selected = trash[:limit] if limit else trash
    cards = {}
    counts = Counter(positive_id(row.get("nmId")) for row in trash)
    spec_counts = Counter(positive_id(size.get("chrtId")) for row in trash
                          for size in (row.get("sizeList") or []) if isinstance(size, dict))
    outcomes = {}
    for row in selected:
        nm = positive_id(row.get("nmId"))
        sizes = valid_sizes(row)
        if nm is None or counts[nm] != 1 or sizes is None:
            outcomes[nm] = "阻止：商品或规格标识缺失、重复"
            continue
        specs = [positive_id(size["chrtId"]) for size in sizes]
        amounts = [stock_at(size, wh) for size in sizes]
        if any(spec_counts[spec] != 1 for spec in specs) or None in amounts:
            outcomes[nm] = "阻止：规格归属或莫斯科仓库存无法确认"
            continue
        cards[nm] = specs if any(amounts) else []

    stopped = False
    for nm, specs in cards.items():
        if stopped:
            outcomes[nm] = "阻止：本店凭证失效"
        elif not args.apply:
            outcomes[nm] = "预览：归零后删除" if specs else "预览：删除"
        elif specs:
            for start in range(0, len(specs), STOCK_CHUNK):
                items = [{"chrtId": spec, "amount": 0} for spec in specs[start:start + STOCK_CHUNK]]
                try:
                    result = bcs.batch_set_stock(sid, wh, items)
                    if result.get("code") != 200:
                        outcomes[nm] = "阻止：归零未受理"
                        break
                except AuthenticationError:
                    outcomes[nm] = "阻止：归零鉴权失败"
                    stopped = True
                    break
                except Exception as error:
                    outcomes[nm] = f"归零结果未知（{type(error).__name__}），已阻止删除"
                    break
            else:
                st["zeroed"] += 1

    ready = [nm for nm in cards if nm not in outcomes]
    for start in range(0, len(ready), DEL_CHUNK):
        batch = ready[start:start + DEL_CHUNK]
        if stopped:
            outcomes.update(dict.fromkeys(batch, "阻止：本店凭证失效"))
            continue
        try:
            outcomes.update(delete_nms(wb_session, batch))
        except (AuthenticationError, common.CookieExpiredError):
            stopped = True
            outcomes.update(dict.fromkeys(batch, "删除鉴权失败，本店已停止"))
        except Exception as error:
            outcomes.update(dict.fromkeys(batch, f"删除结果未知（{type(error).__name__}）"))
    for row in selected:
        nm = positive_id(row.get("nmId"))
        status = outcomes[nm]
        st["total"] += 1
        st["deleted"] += status == "删除已受理"
        st["failed"] += status != "删除已受理" and not status.startswith("预览")
        rows.append({"店铺": name, "ID": nm, "类型": "nmId",
                     "vendorCode": row.get("vendorCode", ""), "结果": status})
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
    if args.limit > 0:
        entries = entries[: args.limit]
    st["total"] = len(entries)
    uuids = [e["uuid"] for e in entries]
    print(f"  [列表] 草稿箱 {len(cards)} 条卡片，本次处理 {len(uuids)} 个")

    if not args.apply:
        for e in entries:
            rows.append({"店铺": name, "ID": e["uuid"], "类型": "nmUuid",
                         "vendorCode": e["vc"], "结果": "DRY将删"})
        print("  [dry-run] 未执行删除（共 %d 条将删）" % len(uuids))
        return st

    outcomes = delete_nm_uuids(wb_session, uuids)
    st["deleted"] = sum(status == "删除已受理" for status in outcomes.values())
    st["failed"] = len(uuids) - st["deleted"]
    for e in entries:
        rows.append({"店铺": name, "ID": e["uuid"], "类型": "nmUuid",
                     "vendorCode": e["vc"], "结果": outcomes[e["uuid"]]})
    if any(status.startswith("鉴权失败") for status in outcomes.values()):
        st["auth_failed"] = True
    return st


def run(args):
    common.ensure_utf8_stdout()
    if args.apply and not getattr(args, "yes", False):
        raise ValueError("永久删除需要 --apply --yes")
    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已填 cookie 的店铺")
        return 1
    if args.limit < 0:
        raise ValueError("--limit 必须大于等于零")
    stopped_shops = set()
    draft_counts = {}
    targets = ["draft", "basket"] if args.target == "all" else [args.target]
    mode_names = {"draft": "草稿箱", "basket": "回收站"}
    wants = {int(x) for x in args.shops.split(",") if x.strip()} if args.shops else None

    # 店铺配对：纯 draft 走 WB 原生列表（无需 BCS 枚举/同步，BCS 凭证失效也能跑）；
    # 回收站候选来自该店 BCS TRASH 清单。
    if targets == ["draft"]:
        pairs = [(s, s.get("shopId")) for s in wb_shops if s.get("shopId")]
        if wants:
            pairs = [p for p in pairs if p[1] in wants]
    else:
        bcs_shops = {s["id"]: s["name"] for s in bcs.fetch_shop_list()}
        pairs = [(s, s["shopId"]) for s in wb_shops if s["shopId"] in bcs_shops]
        if wants:
            pairs = [p for p in pairs if p[1] in wants]

    missing = (wants or set()) - {sid for _, sid in pairs}
    if missing:
        raise ValueError(f"目标店铺缺少已验证的连接：{sorted(missing)}")
    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）")
        return 1
    root_version = cred.root_version
    pair_names = [f"{s['shopName']}({s['shopId']})" for s, _ in pairs]
    print(f"店铺 {len(pairs)} 个: {pair_names}"
          f" | 目标: {' → '.join(mode_names[t] for t in targets)}"
          f"{'' if args.apply else '  [dry-run]'}")

    failures = shop_errors = 0
    for tgt in targets:
        mode = mode_names[tgt]
        rows = []
        totals = {"total": 0, "deleted": 0, "failed": 0, "zeroed": 0}
        print(f"\n########## 清理{mode} ##########")
        for shop, sid in pairs:
            remaining = args.limit - draft_counts.get(sid, 0)
            if sid in stopped_shops or (tgt == "basket" and args.limit and remaining <= 0):
                continue
            try:
                wb_s = wb_api.make_session(shop, root_version)
                if tgt == "draft":
                    st = process_draft(shop, sid, wb_s, args, rows)
                    draft_counts[sid] = st["total"]
                    if st.get("auth_failed"):
                        stopped_shops.add(sid)
                else:
                    st = process_basket(shop, sid, wb_s, args, rows, limit=remaining)
                for k in totals:
                    totals[k] += st[k]
            except (AuthenticationError, common.CookieExpiredError) as e:
                stopped_shops.add(sid)
                shop_errors += 1
                print(f"  [警告] 店铺 {shop['shopName']}: {e}（该店中止，继续下一店）")
            except Exception as e:
                if tgt == "draft" and args.limit:
                    stopped_shops.add(sid)
                shop_errors += 1
                print(f"  [警告] 店铺 {shop['shopName']} 处理失败: {e}")
            time.sleep(0.5)

        failures += totals["failed"]
        print(f"\n[汇总-{mode}] 共 {totals['total']} | "
              f"{'删除已受理 ' + str(totals['deleted']) if args.apply else '预览 ' + str(len(rows))}"
              f" | 失败 {totals['failed']}" + (f" | 归零已受理 {totals['zeroed']}" if tgt == "basket" else ""))
        if rows:
            os.makedirs(config.current.log_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(config.current.log_dir, f"{mode}清理_{ts}.csv")
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["店铺", "ID", "类型", "vendorCode", "结果"])
                w.writeheader()
                w.writerows(rows)
            print(f"[日志] {path}")

    if args.apply:
        common.print_write_hint()
    else:
        print("\n（dry-run 未执行任何删除；确认清单后加 --apply --yes 执行）")
    if shop_errors:
        print(f"[汇总] 店铺异常 {shop_errors}")
    return 1 if failures or shop_errors else 0

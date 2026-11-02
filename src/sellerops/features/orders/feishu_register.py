"""feishu register。"""
from sellerops.integrations.mabang.client import get_mabang_cred as mabang_api_get_mabang_cred
from sellerops.integrations.mabang.orders import fetch_pending_orders as mabang_api_fetch_pending_orders
from sellerops.integrations.mabang.orders import fetch_all_orders as mabang_api_fetch_all_orders
import csv
import json

import re


import time
from sellerops.settings import paths as config
from sellerops.core import utils as common
from sellerops.settings import credentials
from sellerops.features.orders.matching import load_local_mapping as mabang_load_local_mapping
from sellerops.features.orders.matching import parse_order as mabang_parse_order
from sellerops.integrations.feishu.tables import invoke
from sellerops.integrations.feishu.tables import list_records
from sellerops.integrations.feishu.tables import resolve_base
from sellerops.integrations.feishu.tables import resolve_table

ORDER_ID_FIELD = "订单编号"

def _flat_cell(v):
    """单元格值 → 去空格字符串（兼容 list/dict 富文本）"""
    if isinstance(v, list):
        v = "".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
    return "" if v is None else str(v).strip()

def scan_order_ids(base_token, table_id):
    """全表分页读「订单编号」→ 去重集合 + 重复统计（run 去重与存量清理共用）。

    返回 dict:
        ids      set[str]   非空订单编号集合（去重依据）
        total    int        表内记录总行数
        empty    int        「订单编号」为空的行数（不计入 ids）
        unique   int        非空唯一订单编号个数
        dup_ids  dict       重复编号 → 出现次数（仅 >1）
        dup_rows int        多余重复行数（sum(次数-1)）
    """
    rows = list_records(base_token, table_id, field_ids=[ORDER_ID_FIELD])
    counts, empty = {}, 0
    for row in rows:
        v = _flat_cell((row.get("fields") or row).get(ORDER_ID_FIELD))
        if not v:
            empty += 1
            continue
        counts[v] = counts.get(v, 0) + 1
    dup_ids = {k: n for k, n in counts.items() if n > 1}
    return {"ids": set(counts), "total": len(rows), "empty": empty,
            "unique": len(counts), "dup_ids": dup_ids,
            "dup_rows": sum(n - 1 for n in dup_ids.values())}

def fetch_existing_order_ids(base_token, table_id):
    """已登记订单编号集合（去重依据）——全表分页读取，防 2000 条截断。"""
    return scan_order_ids(base_token, table_id)["ids"]

def batch_create(base_token, table_id, records):
    """写入记录（≤200/批，串行）；返回写入条数"""
    ok = 0
    for i in range(0, len(records), 200):
        chunk = records[i:i + 200]
        d = invoke(["+record-batch-create", "--base-token", base_token,
                   "--table-id", table_id],
                  payload={"create_records": chunk})
        record_ids = d.get("record_id_list") or []
        if len(record_ids) != len(chunk) or len(set(record_ids)) != len(chunk):
            raise RuntimeError(
                f"飞书第 {i // 200 + 1} 批创建响应不完整：已确认 {ok} 条，"
                f"本批返回 {len(record_ids)}/{len(chunk)} 条；已中止，勿盲目重放")
        ok += len(record_ids)
        if i + 200 < len(records):
            time.sleep(0.5)
    return ok

QTY_RE = re.compile(r"\*(\d+)\s*$")

def parse_qty(p):
    """从订单摘要尾部 *N 取件数；解析不到默认 1"""
    m = QTY_RE.search(p.get("matched_sku", "")) or QTY_RE.search(p.get("vc", ""))
    if m:
        return int(m.group(1))
    t = p.get("kind")
    return int(t) if (t or "").isdigit() else 1

def _cn_resolver(vc2cn):
    """飞书登记专用：vc → 中文名 三级回退（映射总表 → vc_known 归属池 → 四位前缀码）。

    为什么需要：映射总表只含**当前在架**商品，商品一下架，vc 就从表里消失、
    登记时中文名变空（2026-10-20：74 单黑丝假发受影响）；而 `vc_known.json`
    是历史归属池，仍记得这些已下架商品的归属。
    ⚠ 仅用于填「商品中文名」这一列；马帮匹配/预报仍要求 vc 在映射表（在架），
    避免把已下架商品判成可发货。
    """
    from sellerops.persistence.mappings import MappingRepository

    cache = {"known": None, "prefix": None}

    def resolve(vc):
        vc = str(vc or "").strip()
        if not vc:
            return None
        cn = vc2cn.get(vc)                              # ① 映射总表
        if cn:
            return cn
        if cache["known"] is None:
            cache["known"] = MappingRepository.load_vc_known()
        cn = (cache["known"].get(vc) or {}).get("cn")   # ② vc_known 归属池
        if cn:
            return str(cn).strip()
        if cache["prefix"] is None:
            cache["prefix"] = MappingRepository.load_prefix_map()
        m = re.match(config.VC_PREFIX_RE, vc) if config.VC_PREFIX_RE else None
        if m:                                           # ③ 四位前缀码兜底
            hit = cache["prefix"].get(m.group(1).upper()) or {}
            if hit.get("cn"):
                return str(hit["cn"]).strip()
        return None

    return resolve

def _merge_pending(orders, cred, args):
    """把新增待处理订单并入登记范围，按平台单号去重；days=0 不限日期。

    为什么需要（2026-09-17 用户规则）：只做了匹配、未进预报/上传/交运流程的订单
    （如 NO_SKU=价格表缺库存SKU）只存在于「待处理」tab，orderalllist 拉不到 →
    只按 orderalllist 登记会漏登这些订单（实测 419539845 / 419490094 缺失）。
    这些订单的「库存SKU」允许留空，商品中文名以本地映射表（vc→中文名）为准。
    """
    def _key(o):
        return str(o.get("platformOrderId") or o.get("id") or "")

    try:
        pend = mabang_api_fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    except Exception as e:
        print(f"  [警告] 待处理订单拉取失败（本次仅用 orderalllist 登记）: {str(e)[:120]}")
        return orders
    seen = {_key(o) for o in orders}
    extra = [o for o in pend if _key(o) and _key(o) not in seen]
    scope = f"最近 {args.days} 天" if args.days else "全部新增，不限日期"
    print(f"[合并] 待处理订单（{scope}）{len(pend)} 单；"
          f"其中 orderalllist 未覆盖 {len(extra)} 单已并入登记范围")
    return orders + extra

def build_records(args):
    """查询订单 → 目标店铺全量（含 NO_VC）→ 登记记录；返回 (records, skip_stats, cred)"""
    cred = mabang_api_get_mabang_cred()
    shop_map = cred["shop_map"]
    print(f"[配置] 目标店铺: {'、'.join(f'{k}→{v}' for k, v in shop_map.items())}")
    if args.scope == "all":
        begin = args.begin or args.date
        end = args.end or args.date
        if not (begin and end):
            raise RuntimeError("--scope all 需要指定日期：--date 2026-09-05（单天）"
                               " 或 --begin/--end（区间）")
        print(f"\n[查询] 全部状态订单全量（服务端不支持日期过滤，拉取后本地筛 {begin}~{end}）...")
        orders = mabang_api_fetch_all_orders(cred)
        # 本地按付款日期过滤（paidTime 精确到天）
        in_range, no_paid = [], 0
        for o in orders:
            pd = (o.get("paidTime") or "")[:10]
            if not pd:
                no_paid += 1
                continue
            if begin <= pd <= end:
                in_range.append(o)
        print(f"[过滤] 日期 {begin}~{end} 命中 {len(in_range)} 单"
              f"（无付款时间 {no_paid} 单不计）")
        orders = in_range
    elif args.scope == "latest":
        # 默认：最近 500 条全状态订单（最新在前）+「待处理订单」两路合并（见 _merge_pending）
        print("\n[查询] 最近 500 条全状态订单（orderalllist，最新在前）...")
        orders = mabang_api_fetch_all_orders(cred, page_size=500, max_pages=1)
        if not getattr(args, "no_pending", False):
            orders = _merge_pending(orders, cred, args)
    else:
        scope = f"最近 {args.days} 天" if args.days else "全部新增，不限日期"
        print(f"\n[查询] 待处理订单（{scope}）...")
        orders = mabang_api_fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    if not orders:
        return [], {"空": 0}, {}, cred

    vc2cn, cn2sku = mabang_load_local_mapping()
    resolve_cn = _cn_resolver(vc2cn)      # 登记填名三级回退（映射表 → vc_known → 前缀码）

    # 各店快照 vendorCode→nmId（wb编号=下单店铺自己的码）
    shop_ids = {}
    for shop, label in shop_map.items():
        m = re.search(r"\((\d+)\)", label)
        if m:
            shop_ids[shop] = int(m.group(1))
    snaps = {}
    for shop, sid in shop_ids.items():
        path = config.shop_json_path(sid)
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
        except FileNotFoundError:
            print(f"  [警告] 店{sid} 快照不存在（wb编号/商品链接将留空）: {path}")
            continue
        if isinstance(rows, dict):
            rows = rows.get("rows") or []
        snaps[shop] = {r.get("vendorCode"): r.get("nmId") for r in rows if r.get("vendorCode")}

    # 已取消订单排除（WB 门户取消单；马帮不同步取消单，防御性过滤）
    canceled = set()
    for shop, sid in shop_ids.items():
        try:
            from sellerops.integrations.wildberries import client as wb_api
            ids = wb_api.fetch_canceled_ids(sid)

            canceled |= ids
            if ids:
                print(f"  [取消单] 店{sid}: {len(ids)} 单已取消")
        except Exception as e:
            print(f"  [警告] 店{sid} 取消单查询失败（跳过排除）: {e}")

    records, skip, notice = [], {}, {}
    for o in orders:
        p = mabang_parse_order(o)
        if p["shop"] not in shop_map:
            skip["非目标店铺"] = skip.get("非目标店铺", 0) + 1
            continue
        if str(p["platform_order_id"]) in canceled:
            skip["已取消"] = skip.get("已取消", 0) + 1
            continue
        if not p["platform_order_id"]:
            skip["无平台单号"] = skip.get("无平台单号", 0) + 1
            continue
        cn = resolve_cn(p["vc"])
        if p["vc"] and cn is None:
            # 仍会登记（用户规则：以本地数据为准，查不到就留空），仅计数提示
            notice["VC不在映射表(仍登记·中文名留空)"] = notice.get("VC不在映射表(仍登记·中文名留空)", 0) + 1
        elif not p["vc"]:
            notice["无BCS编号(仍登记)"] = notice.get("无BCS编号(仍登记)", 0) + 1
        wb_code = ""
        if p["vc"]:
            wb_code = str(snaps.get(p["shop"], {}).get(p["vc"]) or "")
        # 店铺写入短名（与表选项一致）：shop_map 值「袁州1(9352)」→「袁州1」
        m_label = re.match(r"(.+?)\(\d+\)", shop_map[p["shop"]])
        shop_label = m_label.group(1).strip() if m_label else shop_map[p["shop"]]
        paid = p.get("paid_time") or ""
        mdate = re.match(r"(\d{4}-\d{2}-\d{2})", paid)
        mtime = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", paid)
        rec = {
            "订单编号": str(p["platform_order_id"]),
            # 下单（付款）时间，精确到分钟
            "日期": mtime.group(1) if mtime else (f"{mdate.group(1)} 00:00" if mdate else None),
            "店铺": [shop_label],
            "BCS编号": p["vc"] or None,
            "商品中文名": (cn or None) if p["vc"] else None,
            # 库存SKU：以本地商品价格表「库存SKU」列为准（用户规则 2026-09-17）；
            # 本地查不到（含只匹配未走完预报/上传/交运流程的单）→ 留空，
            # 不再回写马帮系统的匹配值（避免 BCS-FSCH-982897107 / BCS-xxx-40-56 这类非法值）
            "库存SKU": (cn2sku.get(cn) or None) if cn else None,
            "wb编号": wb_code or None,
            "商品链接": (f"https://www.wildberries.ru/catalog/{wb_code}/detail.aspx"
                        if wb_code else None),
            "订单量": parse_qty(p),
        }
        records.append(rec)
    return records, skip, notice, cred

CSV_FIELDS = ["订单编号", "日期", "店铺", "BCS编号", "商品中文名", "库存SKU", "wb编号", "订单量", "结果"]

def write_csv(records, existing_ids):
    def _w(f):
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            row = {k: r.get(k) for k in CSV_FIELDS[:-1]}
            if r.get("店铺"):
                row["店铺"] = ";".join(r["店铺"])
            row["结果"] = "跳过-已登记" if str(r["订单编号"]) in existing_ids else (
                "已写入" if r.get("_written") else "待写入")
            w.writerow(row)

    return common.write_csv_preferred(config.LOG_DIR, "飞书订单登记_{ts}.csv",
                                      "feishu_order_register_{ts}.csv",
                                      time.strftime("%Y%m%d_%H%M%S"), _w)

def run(args):
    common.ensure_utf8_stdout()
    url = args.url or credentials.get().feishu_base_url()
    if not url or not args.table:
        print("[错误] 未提供 --url 且配置 feishu.base_url 缺失（--table 亦不可为空）")
        return 1
    base_token = resolve_base(url)
    table_id = resolve_table(base_token, args.table)
    print(f"[表格] base_token={base_token} table_id={table_id}（{args.table}）")

    records, skip, notice, _cred = build_records(args)
    if not records:
        print("[完成] 无可登记订单")
        return 0

    scan = scan_order_ids(base_token, table_id)
    existing = scan["ids"]
    print(f"\n[去重] 表内记录 {scan['total']} 行 ｜ 唯一订单编号 {scan['unique']} 个"
          f" ｜ 空编号 {scan['empty']} 行")
    if scan["dup_ids"]:
        top = sorted(scan["dup_ids"].items(), key=lambda kv: -kv[1])[:5]
        print(f"[警告] 表内已存在重复订单编号 {len(scan['dup_ids'])} 个"
              f"（多余 {scan['dup_rows']} 行）：" + "、".join(f"{k}×{n}" for k, n in top) +
              " —— 建议执行 `python -m sellerops feishu-register-dedup` 清理")
    # 双重去重：① 表内已登记（existing）② 本批 records 自身（seen）
    seen, todo, dup_in_batch = set(), [], 0
    for r in records:
        k = str(r["订单编号"])
        if k in existing:
            continue
        if k in seen:
            dup_in_batch += 1
            continue
        seen.add(k)
        todo.append(r)
    dup = len(records) - len(todo)
    print(f"[圈定] 目标店铺订单 {len(records)} 单：将登记 {len(todo)} 单，"
          f"已登记跳过 {dup - dup_in_batch} 单，本批内重复 {dup_in_batch} 单；真排除: " +
          (" / ".join(f"{k}={v}" for k, v in sorted(skip.items())) or "无") +
          ("；字段留空: " + " / ".join(f"{k}={v}" for k, v in sorted(notice.items())) if notice else ""))
    # 供调用方（如 mabang-process / 人工）读取本次新登记订单
    args.registered_new = todo if args.apply else []
    for r in todo[:15]:
        print(f"  {r['订单编号']} | {r['店铺'][0] if r['店铺'] else '-'} | "
              f"{r['BCS编号'] or '(无BCS)'} {r['商品中文名'] or ''} wb={r['wb编号'] or '-'} "
              f"件数={r['订单量']}")
    if len(todo) > 15:
        print(f"  ... 其余 {len(todo) - 15} 单见 CSV")

    if not args.apply:
        print("\n（dry-run 未写入；确认无误后加 --apply 登记）")
    elif todo:
        # 写入按日期从上到下（升序），自然追加到表尾
        todo.sort(key=lambda r: (r.get("日期") or "", str(r["订单编号"])))
        n = batch_create(base_token, table_id, todo)
        print(f"\n[写入] 成功登记 {n} 条记录")
        for r in todo:
            r["_written"] = True
    else:
        print("\n[写入] 无新增（全部已登记）")

    csv_path = write_csv(records, existing)
    print(f"[汇总] 明细: {csv_path or '（未生成，见上方警告）'}")
    return 0

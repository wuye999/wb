# -*- coding: utf-8 -*-
"""
wb_ops 马帮订单登记到飞书多维表格「订单登记」（原 爆品登记）

流程：
  1. 拉取登记数据源（scope=latest 时**两路合并**）：
       orderalllist 最近 500 条全状态订单 + 待处理订单（order.oTc tabId=7，最近 --days 天）
       —— 合并目的：只做了匹配、未进预报/上传/交运流程的订单（如 NO_SKU）只出现在「待处理」，
          orderalllist 拉不到；--no-pending 可关闭该合并
  2. 逐单构建登记记录（**一律以本地数据为准**）：
       订单编号=平台单号（去重键）；日期=付款时间；店铺=shop_map 映射（袁州N(935x)）
       BCS编号=vendorCode；商品中文名=本地映射表反查（查不到留空，仍登记）
       库存SKU=本地商品价格表「库存SKU」列（第 8 列）；查不到一律留空（不回写马帮系统匹配值）
       wb编号=下单店铺快照 vendorCode→nmId（≠映射表主店码）；商品链接=WB 详情页
       订单量=订单摘要件数（解析不到默认 1）
  3. 按「订单编号」去重：已登记的不重复登记
     ⚠ 去重集必须**全表分页读取**（scan_order_ids / _record_list_all）。
     2026-10 事故：旧实现只读 ndjson 第一页（默认上限 2000 条、按插入序最早在前），
     表记录数过 2000 行后去重集被冻结在「最老 2000 行的 1991 个编号」，
     导致新登记订单每次运行都被判为未登记而重复写入（实测 6429 行仅 3496 个唯一编号）。
     存量清理见 wb.py feishu-register-dedup。
  4. dry-run 默认；--apply 经 lark-cli record-batch-create 写入（≤200/批）

变量：--url 表格地址、--table 表格名（不硬编码）。
"""
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from wb_ops import config
from wb_ops import common
from wb_ops import credentials
from wb_ops.services.order import mabang

# Windows 下 subprocess 需要完整 .cmd 路径（PATH 里的 sh shim 无法直接运行）
LARK = shutil.which("lark-cli") or shutil.which("lark-cli.cmd") or "lark-cli.cmd"


# ---------------- lark-cli 封装 ----------------
def _lark(args, payload=None, timeout=120):
    """调用 lark-cli base 子命令；payload 走临时文件 --json @file 避免转义问题"""
    if not (shutil.which(LARK) or (os.path.isabs(LARK) and os.path.exists(LARK))):
        raise RuntimeError("未在系统环境中找到 lark-cli 命令行工具，飞书多维表格同步功能需安装飞书 CLI：npm install -g @lark-cli/cli 并配置 PATH")
    cmd = [LARK, "base"] + args + ["--as", "user"]
    tmp = None
    if payload is not None:
        tmp = os.path.join(config.LOG_DIR, "_lark_payload.json")
        os.makedirs(config.LOG_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        cmd += ["--json", f"@{tmp}"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if tmp and os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    out = p.stdout or ""
    try:
        d = json.loads(out)
    except Exception:
        raise RuntimeError(f"lark-cli 输出非 JSON: {(p.stderr or out)[:200]}")
    if "ok" in d and not d.get("ok"):
        raise RuntimeError(f"lark-cli 失败: {json.dumps(d.get('error'), ensure_ascii=False)[:200]}")
    # 带 --output 的调用成功时返回裸 manifest JSON（无 ok/data 包装）；常规调用取 data 内层
    if "data" in d and isinstance(d["data"], dict):
        return d["data"]
    return d if isinstance(d, dict) else {}


def resolve_base(url):
    """表格地址 → base_token"""
    d = _lark(["+url-resolve", "--url", url])
    if d.get("resource_type") != "bitable":
        raise RuntimeError(f"链接不是多维表格: resource_type={d.get('resource_type')}")
    return d["base_token"]


def resolve_table(base_token, table_name):
    """表格名 → table_id（精确匹配）"""
    d = _lark(["+table-list", "--base-token", base_token])
    for t in d.get("tables") or []:
        if t.get("name") == table_name:
            return t["id"]
    names = [t.get("name") for t in d.get("tables") or []]
    raise RuntimeError(f"表格「{table_name}」不存在；现有表格: {names}")


ORDER_ID_FIELD = "订单编号"


def _record_list_all(base_token, table_id, field_ids=None):
    """分页读飞书表全部记录 → list[dict]（每行含 record_id + fields）。

    为什么必须分页：lark-cli `+record-list --format ndjson` **单次上限 2000 条**
    （help：`--limit ... 1-2000 for ndjson; omitted limit uses 2000`），且默认按
    插入顺序（最早在前）返回。只读第一页会静默截断 —— 本文件 2026-10 的
    「订单登记重复写入」事故即由此而来（表过 2000 行后去重集被冻结）。
    本函数按 manifest 的 has_more/next_offset 翻页读完为止。

    Args:
        base_token / table_id: 表定位。
        field_ids: 可选字段投影（列名或列 id），非空时逐项 --field-id 以减小 ndjson 体积。

    Returns:
        list[dict]：每行形如 {"record_id": ..., "fields": {列名: 值}}。

    Raises:
        RuntimeError: manifest 报 has_more 却无 next_offset、或某页 0 行却报 has_more
            —— 宁可报错中止也不静默漏读。
    """
    rows, offset, page = [], 0, 0
    while True:
        # 临时文件带 pid + 页码：并发/重入不互相覆盖（旧实现固定名 _lark_existing.ndjson）
        out = os.path.join(tempfile.gettempdir(), f"_lark_rl_{os.getpid()}_{page}.ndjson")
        man_path = out.replace(".ndjson", ".manifest.json")
        for p in (out, man_path):
            if os.path.exists(p):
                os.remove(p)
        cmd = ["+record-list", "--base-token", base_token, "--table-id", table_id,
               "--limit", "2000", "--offset", str(offset),
               "--format", "ndjson", "--output", out, "--overwrite"]
        for fid in (field_ids or []):
            cmd += ["--field-id", fid]
        man = _lark(cmd)                      # 带 --output 的调用返回裸 manifest JSON
        before = len(rows)
        if os.path.exists(out):
            with open(out, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        for p in (out, man_path):
            try:
                os.remove(p)
            except OSError:
                pass
        if not man.get("has_more"):
            return rows
        nxt = man.get("next_offset")
        if nxt in (None, ""):
            raise RuntimeError(f"record-list 报告 has_more 但未给 next_offset"
                               f"（已读 {len(rows)} 条），为避免漏读已中止")
        if len(rows) == before:
            raise RuntimeError(f"record-list 第 {page + 1} 页返回 0 行却报告 has_more，"
                               f"异常中止（已读 {len(rows)} 条）")
        offset, page = int(nxt), page + 1
        time.sleep(0.3)


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
    rows = _record_list_all(base_token, table_id, field_ids=[ORDER_ID_FIELD])
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
        d = _lark(["+record-batch-create", "--base-token", base_token,
                   "--table-id", table_id],
                  payload={"create_records": chunk})
        ok += len((d.get("record_id_list") or []) or chunk)
        if i + 200 < len(records):
            time.sleep(0.5)
    return ok


# ---------------- 记录构建 ----------------
QTY_RE = re.compile(r"\*(\d+)\s*$")


def parse_qty(p):
    """从订单摘要尾部 *N 取件数；解析不到默认 1"""
    m = QTY_RE.search(p.get("matched_sku", "")) or QTY_RE.search(p.get("vc", ""))
    if m:
        return int(m.group(1))
    t = p.get("kind")
    return int(t) if (t or "").isdigit() else 1


def _merge_pending(orders, cred, args):
    """把「待处理订单」（order.oTc tabId=7，最近 args.days 天）并入登记范围，按平台单号去重。

    为什么需要（2026-09-17 用户规则）：只做了匹配、未进预报/上传/交运流程的订单
    （如 NO_SKU=价格表缺库存SKU）只存在于「待处理」tab，orderalllist 拉不到 →
    只按 orderalllist 登记会漏登这些订单（实测 419539845 / 419490094 缺失）。
    这些订单的「库存SKU」允许留空，商品中文名以本地映射表（vc→中文名）为准。
    """
    def _key(o):
        return str(o.get("platformOrderId") or o.get("id") or "")

    try:
        pend = mabang.fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    except Exception as e:
        print(f"  [警告] 待处理订单拉取失败（本次仅用 orderalllist 登记）: {str(e)[:120]}")
        return orders
    seen = {_key(o) for o in orders}
    extra = [o for o in pend if _key(o) and _key(o) not in seen]
    print(f"[合并] 待处理订单（最近 {args.days} 天）{len(pend)} 单；"
          f"其中 orderalllist 未覆盖 {len(extra)} 单已并入登记范围")
    return orders + extra


def build_records(args):
    """查询订单 → 目标店铺全量（含 NO_VC）→ 登记记录；返回 (records, skip_stats, cred)"""
    cred = mabang._mabang_cred()
    shop_map = cred["shop_map"]
    print(f"[配置] 目标店铺: {'、'.join(f'{k}→{v}' for k, v in shop_map.items())}")
    if args.scope == "all":
        begin = args.begin or args.date
        end = args.end or args.date
        if not (begin and end):
            raise RuntimeError("--scope all 需要指定日期：--date 2026-09-05（单天）"
                               " 或 --begin/--end（区间）")
        print(f"\n[查询] 全部状态订单全量（服务端不支持日期过滤，拉取后本地筛 {begin}~{end}）...")
        orders = mabang.fetch_all_orders(cred)
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
        orders = mabang.fetch_all_orders(cred, page_size=500, max_pages=1)
        if not getattr(args, "no_pending", False):
            orders = _merge_pending(orders, cred, args)
    else:
        print(f"\n[查询] 待处理订单（最近 {args.days} 天）...")
        orders = mabang.fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    if not orders:
        return [], {"空": 0}, {}, cred

    vc2cn, cn2sku = mabang.load_local_mapping()

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
            from wb_ops.adapters import wb_client as wb_api
            ids = wb_api.fetch_canceled_ids(sid)

            canceled |= ids
            if ids:
                print(f"  [取消单] 店{sid}: {len(ids)} 单已取消")
        except Exception as e:
            print(f"  [警告] 店{sid} 取消单查询失败（跳过排除）: {e}")

    records, skip, notice = [], {}, {}
    for o in orders:
        p = mabang.parse_order(o)
        if p["shop"] not in shop_map:
            skip["非目标店铺"] = skip.get("非目标店铺", 0) + 1
            continue
        if str(p["platform_order_id"]) in canceled:
            skip["已取消"] = skip.get("已取消", 0) + 1
            continue
        if not p["platform_order_id"]:
            skip["无平台单号"] = skip.get("无平台单号", 0) + 1
            continue
        cn = vc2cn.get(p["vc"])
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
              " —— 建议执行 `python wb.py feishu-register-dedup` 清理")
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

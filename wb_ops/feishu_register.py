# -*- coding: utf-8 -*-
"""
wb_ops 马帮订单登记到飞书多维表格「订单登记」（原 爆品登记）

流程：
  1. 查询马帮待处理订单（复用 mabang.py：目标店铺 shop_map 过滤）
  2. 逐单构建登记记录：
       订单编号=平台单号（去重键）；日期=付款时间；店铺=shop_map 映射（袁州N(935x)）
       BCS编号=vendorCode；商品中文名=映射表反查（NO_VC 留空）
       wb编号=下单店铺快照 vendorCode→nmId（≠映射表主店码）；商品链接=WB 详情页
       订单量=订单摘要件数（解析不到默认 1）
  3. 按「订单编号」去重：已登记的不重复登记
  4. dry-run 默认；--apply 经 lark-cli record-batch-create 写入（≤200/批）

变量：--url 表格地址、--table 表格名（不硬编码）。
"""
import csv
import json
import os
import re
import shutil
import subprocess
import time

from . import config
from . import common
from . import mabang

# Windows 下 subprocess 需要完整 .cmd 路径（PATH 里的 sh shim 无法直接运行）
LARK = shutil.which("lark-cli") or shutil.which("lark-cli.cmd") or "lark-cli.cmd"


# ---------------- lark-cli 封装 ----------------
def _lark(args, payload=None, timeout=120):
    """调用 lark-cli base 子命令；payload 走临时文件 --json @file 避免转义问题"""
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


def fetch_existing_order_ids(base_token, table_id):
    """已登记订单编号集合（去重依据）"""
    # lark-cli --output 会生成 .manifest.json 附属文件且不允许覆盖，固定文件名 + --overwrite
    tmp_out = os.path.join(config.LOG_DIR, "_lark_existing.ndjson")
    _lark(["+record-list", "--base-token", base_token, "--table-id", table_id,
           "--field-id", "订单编号", "--format", "ndjson", "--output", tmp_out,
           "--overwrite"])
    ids = set()
    if os.path.exists(tmp_out):
        with open(tmp_out, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                v = row.get("fields", row).get("订单编号")
                if isinstance(v, list):
                    v = "".join(str(x) for x in v)
                if v:
                    ids.add(str(v).strip())
    for p in (tmp_out, tmp_out.replace(".ndjson", ".manifest.json")):
        try:
            os.remove(p)
        except OSError:
            pass
    return ids

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
    else:
        print(f"\n[查询] 待处理订单（最近 {args.days} 天）...")
        orders = mabang.fetch_pending_orders(cred, days=args.days, page_size=args.page_size)
    if not orders:
        return [], {"空": 0}, cred

    vc2cn, _cn2sku = mabang.load_local_mapping()

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
            from . import remote_wh
            ids = remote_wh.fetch_canceled_ids(sid)
            canceled |= ids
            if ids:
                print(f"  [取消单] 店{sid}: {len(ids)} 单已取消")
        except Exception as e:
            print(f"  [警告] 店{sid} 取消单查询失败（跳过排除）: {e}")

    records, skip = [], {}
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
            skip["VC不在映射表"] = skip.get("VC不在映射表", 0) + 1
        elif not p["vc"]:
            skip["无BCS编号"] = skip.get("无BCS编号", 0) + 1
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
            # 库存SKU = 马帮订单列表实际选择的库存SKU（order_ellipsis_title）
            "库存SKU": (p["matched_sku"] or None) or None,
            "wb编号": wb_code or None,
            "商品链接": (f"https://www.wildberries.ru/catalog/{wb_code}/detail.aspx"
                        if wb_code else None),
            "订单量": parse_qty(p),
        }
        records.append(rec)
    return records, skip, cred


CSV_FIELDS = ["订单编号", "日期", "店铺", "BCS编号", "商品中文名", "库存SKU", "wb编号", "订单量", "结果"]


def write_csv(records, existing_ids):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"飞书订单登记_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            row = {k: r.get(k) for k in CSV_FIELDS[:-1]}
            if r.get("店铺"):
                row["店铺"] = ";".join(r["店铺"])
            row["结果"] = "跳过-已登记" if str(r["订单编号"]) in existing_ids else (
                "已写入" if r.get("_written") else "待写入")
            w.writerow(row)
    return path


def run(args):
    common.ensure_utf8_stdout()
    if not args.url or not args.table:
        print("[错误] 必须提供 --url 表格地址 与 --table 表格名")
        return 1
    base_token = resolve_base(args.url)
    table_id = resolve_table(base_token, args.table)
    print(f"[表格] base_token={base_token} table_id={table_id}（{args.table}）")

    records, skip, _cred = build_records(args)
    if not records:
        print("[完成] 无可登记订单")
        return 0

    existing = fetch_existing_order_ids(base_token, table_id)
    print(f"\n[去重] 表内已有订单编号 {len(existing)} 条")
    todo = [r for r in records if str(r["订单编号"]) not in existing]
    dup = len(records) - len(todo)
    print(f"[圈定] 目标店铺订单 {len(records)} 单：将登记 {len(todo)} 单，"
          f"已登记跳过 {dup} 单；排除: " +
          (" / ".join(f"{k}={v}" for k, v in sorted(skip.items())) or "无"))
    # 供编排层（orders-pipeline）读取本次新登记订单
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
    print(f"[汇总] 明细: {csv_path}")
    return 0

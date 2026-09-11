# -*- coding: utf-8 -*-
"""
wb_ops 马帮库存登记：拉取马帮全部库存 SKU → 全量重建飞书「马帮库存登记表」
数据源（2026-09-10 抓包实测：api/网络请求/马帮获取库存SKU的库存.har）：
- POST aamz.mabangerp.com/index.php?mod=stock.getStockList（aamz cookie 鉴权）
  body: searchKey=Stock_stockSku&operate=likeStart&status=3&stockOrderby=a.stockQuantity desc
  （page/rowsPerPage 留空 = 返回全量）→ stockData[]：stockSku/nameCN/stockQuantity/statusText/stockPicture
写入：全量重建（先清空表再写入），图片下载后经 +record-upload-attachment 传「图」附件列
约定：默认 dry-run；--apply 真正执行；库存总量可为负，原样写入
"""
import json
import os
import re
import shutil
import tempfile
import time
from datetime import timedelta
from collections import Counter
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests

from . import common
from . import credentials
from . import mabang
from .feishu_register import _lark, resolve_base, resolve_table

STOCK_LIST_URL = "https://aamz.mabangerp.com/index.php"


def fetch_stock_list(cred):
    """拉取马帮全部库存 SKU → list[dict{stockSku,nameCN,stockQuantity,statusText,stockPicture}]"""
    form = {"searchKey": "Stock_stockSku", "operate": "likeStart", "orderBys[]": "",
            "Stock_stockSku": "", "Stock_nameCN": "", "Stock_nameEN": "",
            "Stock_defaultRetailNameCn": "", "StockPlus_financial": "",
            "search-content": "库存SKU", "searchValue": "", "status": "3",
            "parentCategoryId": "", "categoryId": "", "third_category_id": "",
            "parentBrandId": "", "list-brandId": "", "labelId": "", "buyerId": "",
            "developerIdM": "", "dev_assistant": "", "artDesignerId": "", "salesId": "",
            "defaultStockWarehouseDetailId": "", "livenessType": "", "isNewType": "",
            "stock_type": "", "isMachining": "", "showstart": "1", "isCloud": "",
            "isGift": "", "exceptionDeclaration": "", "singleWarehouseType": "",
            "isGoogsExpireManageSearch": "", "page": "", "rowsPerPage": "",
            "stockOrderby": "a.stockQuantity desc"}
    r = requests.post(STOCK_LIST_URL, params={"mod": "stock.getStockList"},
                      headers=mabang._aamz_headers(cred), data=form, timeout=120)
    r.raise_for_status()
    text = r.text.lstrip("﻿")
    d = json.loads(text)
    if not d.get("success"):
        raise RuntimeError(f"stock.getStockList 返回失败: {str(d)[:150]}")
    out = []
    for it in d.get("stockData") or []:
        out.append({"stockSku": it.get("stockSku") or "",
                    "nameCN": it.get("nameCN") or "",
                    "stockQuantity": it.get("stockQuantity"),
                    "statusText": it.get("statusText") or "",
                    "stockPicture": it.get("stockPicture") or ""})
    return out


def _read_all_record_ids(base_token, table_id):
    """读全表现有记录 ID 列表（清空前用）"""
    out = os.path.join(tempfile.gettempdir(), "_mbstock_read.ndjson")
    if os.path.exists(out):
        os.remove(out)
    _lark(["+record-list", "--base-token", base_token, "--table-id", table_id,
           "--format", "ndjson", "--output", out, "--overwrite"])
    res = []
    with open(out, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                rid = d.get("record_id")
                if rid:
                    res.append(str(rid))
    try:
        os.remove(out)
        os.remove(out.replace(".ndjson", ".manifest.json"))
    except OSError:
        pass
    return res


def _create_records(base_token, table_id, items):
    """分批创建记录，返回 [(record_id, item)]（顺序对应）"""
    pairs = []
    payload_rows = [{"库存SKU": it["stockSku"] or None,
                     "商品中文名": it["nameCN"] or None,
                     "库存总量": str(it["stockQuantity"]) if it["stockQuantity"] is not None else None,
                     "状态": it["statusText"] or None} for it in items]
    for i in range(0, len(payload_rows), 200):
        chunk = payload_rows[i:i + 200]
        d = _lark(["+record-batch-create", "--base-token", base_token,
                   "--table-id", table_id],
                  payload={"create_records": chunk})
        rid_list = d.get("record_id_list") or []
        for rid, it in zip(rid_list, items[i:i + 200]):
            pairs.append((str(rid), it))
        time.sleep(0.5)
    return pairs


# ---------------- 每日新订单量列 ----------------

def _field_id_by_name(base_token, table_id, name):
    """field-list 查列 id（走 fr._lark，二进制解析已验证）；失败返回 None"""
    try:
        d = _lark(["+field-list", "--base-token", base_token,
                   "--table-id", table_id])
        # fr._lark 已剥壳：字段列表直接在顶层 d["fields"]
        for f in d.get("fields") or []:
            if f.get("name") == name:
                return f.get("id")
    except Exception as e:
        print(f"  [警告] field-list 查询失败（按新建处理）: {str(e)[:60]}")
    return None


def _ensure_date_field(base_token, table_id, name, cache):
    """确保日期列（text）存在；返回 field name（写记录用列名即可）。"""
    if name in cache:
        return cache[name]
    fid = _field_id_by_name(base_token, table_id, name)
    if not fid:
        d = _lark(["+field-create", "--base-token", base_token,
                   "--table-id", table_id, "--as", "user"],
                  payload={"name": name, "type": "text"})
        fid = d.get("id")
        print(f"  [新列] {name}（id={fid}）")
    else:
        print(f"  [列已存在] {name}")
    cache[name] = fid or name
    return cache[name]


def read_orders_daily(base_token, orders_table_id, begin, end):
    """读「订单登记」→ {date: {库存SKU: 总件数}}；无库存SKU 的订单单独计数"""
    out = os.path.join(tempfile.gettempdir(), "_mbsku_orders.ndjson")
    if os.path.exists(out):
        os.remove(out)
    _lark(["+record-list", "--base-token", base_token, "--table-id", orders_table_id,
           "--format", "ndjson", "--output", out, "--overwrite"])
    daily, no_sku = {}, Counter()
    with open(out, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            f = d.get("fields") or d

            def _sv(v):
                if v is None:
                    return ""
                if isinstance(v, list):
                    v = "".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
                return str(v).strip()

            dt = _sv(f.get("日期"))[:10]
            if not dt or dt < begin or dt > end:
                continue
            sku = _sv(f.get("库存SKU"))
            try:
                qty = int(float(_sv(f.get("订单量")) or 1))
            except ValueError:
                qty = 1
            if not sku:
                no_sku[dt] += qty
                continue
            daily.setdefault(dt, Counter())
            daily[dt][sku] += qty
    try:
        os.remove(out)
        os.remove(out.replace(".ndjson", ".manifest.json"))
    except OSError:
        pass
    return daily, no_sku


def read_stock_records(base_token, table_id):
    """读「马帮库存登记表」→ [(record_id, 库存SKU, fields_dict)]"""
    out = os.path.join(tempfile.gettempdir(), "_mbsku_stock.ndjson")
    if os.path.exists(out):
        os.remove(out)
    _lark(["+record-list", "--base-token", base_token, "--table-id", table_id,
           "--format", "ndjson", "--output", out, "--overwrite"])
    res = []
    with open(out, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            f = d.get("fields") or d
            rid = d.get("record_id")

            def _sv(v, _f=f):
                if v is None:
                    return ""
                if isinstance(v, list):
                    v = "".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
                return str(v).strip()

            if rid:
                res.append((str(rid), _sv(f.get("库存SKU")), f))
    try:
        os.remove(out)
        os.remove(out.replace(".ndjson", ".manifest.json"))
    except OSError:
        pass
    return res


DATE_COL_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日新订单量$")


def _list_date_columns(base_token, table_id, year):
    """field-list → {(month, day): 列名}（仅当年日期列）"""
    d = _lark(["+field-list", "--base-token", base_token, "--table-id", table_id])
    cols = {}
    for f in d.get("fields") or []:
        m = DATE_COL_RE.match(f.get("name") or "")
        if m:
            cols[(int(m.group(1)), int(m.group(2)))] = f["name"]
    return cols


def _date_range(begin, end):
    """[begin, end] 闭区间日期列表 → [(YYYY-MM-DD, (month, day))]"""
    b = datetime.strptime(begin, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    out, cur = [], b
    while cur <= e:
        out.append((cur.strftime("%Y-%m-%d"), (cur.month, cur.day)))
        cur += timedelta(days=1)
    return out


def run_daily(args):
    """马帮库存登记表日期列管理：
    默认（不带 --begin/--date）：不删旧列、只建今天列（缺失时）、更新所有已存在日期列数据；
    显式 --begin（或 --date）：删除 begin 之前的日期列 + 补建区间缺列 + 填充。
    填充口径：有订单的 SKU 写数量；无订单的 SKU 留空（重跑时清空残留旧值，含旧 "0" 与旧非零）；
      总新增订单量 = 本次运行区间各日之和，同口径清空。"""
    common.ensure_utf8_stdout()
    url = args.url or credentials.get().feishu_base_url()
    if not url:
        print("[错误] 未提供 --url 且配置 feishu.base_url 缺失")
        return 1
    base_token = resolve_base(url)
    table_id = resolve_table(base_token, args.table)
    orders_tid = resolve_table(base_token, args.orders_table)

    today = time.strftime("%Y-%m-%d")
    explicit = bool(args.begin or args.date)
    begin = args.begin or args.date or today
    end = args.end or args.date or today
    if begin > end:
        begin, end = end, begin
    print(f"[表格] 库存表={table_id} 订单表={orders_tid}")
    print(f"[日期] {begin} ~ {end}（explicit={explicit}）")

    stock_rows = read_stock_records(base_token, table_id)
    print(f"[库存] 马帮库存登记表 {len(stock_rows)} 条记录")

    year = int(begin[:4])
    existing = _list_date_columns(base_token, table_id, year)

    # 删除清单：仅显式 --begin/--date 时删除 begin 之前的日期列
    del_cols = []
    if explicit:
        del_cols = [nm for (mo, dy), nm in sorted(existing.items())
                    if datetime(year, mo, dy).strftime("%Y-%m-%d") < begin]

    # 建列清单：显式=begin~end 区间缺列；默认=仅今天缺列
    rng = _date_range(begin, end)
    if not explicit:
        rng = [(today, (int(today[5:7]), int(today[8:10])))]

    if not args.apply:
        missing = [f"{mo}月{dy}日新订单量" for _, (mo, dy) in rng
                   if (mo, dy) not in existing]
        print(f"[dry-run] 将删除旧列 {len(del_cols)} 个: {del_cols or '无'}")
        print(f"[dry-run] 将新建列 {len(missing)} 个: {missing or '无'}")
        upd_dates = sorted({f"{mo}月{dy}日" for _, (mo, dy) in rng} |
                           {f"{mo}月{dy}日" for (mo, dy) in existing
                            if datetime(year, mo, dy).strftime("%Y-%m-%d") >= begin})
        print(f"[dry-run] 将更新数据列 {len(upd_dates)} 个: {upd_dates or '无'}")
        print("（dry-run 未写入；确认无误后加 --apply 执行）")
        return 0

    for nm in del_cols:
        _lark(["+field-delete", "--base-token", base_token, "--table-id", table_id,
               "--field-id", nm, "--yes"])
        print(f"  [删列] {nm}")
    if del_cols:
        time.sleep(0.5)
        existing = _list_date_columns(base_token, table_id, year)

    for _, (mo, dy) in rng:
        col = f"{mo}月{dy}日新订单量"
        if (mo, dy) not in existing:
            _lark(["+field-create", "--base-token", base_token, "--table-id", table_id,
                   "--as", "user"], payload={"name": col, "type": "text"})
            print(f"  [建列] {col}")
            time.sleep(0.3)
            existing[(mo, dy)] = col

    # 统计区间：覆盖所有目标日期列对应日期 ~ 今天（保证已有列更新数据完整）
    all_dates = [datetime(year, mo, dy).strftime("%Y-%m-%d") for (mo, dy), _ in existing.items()]
    all_dates += [d for d, _ in rng]
    stats_begin = min(all_dates + [begin, today])[:10]
    daily, no_sku = read_orders_daily(base_token, orders_tid, stats_begin, today)
    print(f"[订单] 统计区间 {stats_begin}~{today}："
          f"{sum(sum(c.values()) for c in daily.values())} 件；"
          f"无库存SKU 未归属: " +
          ("、".join(f"{k}={v}" for k, v in sorted(no_sku.items())) if no_sku else "无"))

    # 更新/填充：目标列 = rng 列 + 存活的所有已存在日期列
    targets = {(mo, dy): col for (mo, dy), col in existing.items()}
    for _, (mo, dy) in rng:
        targets[(mo, dy)] = f"{mo}月{dy}日新订单量"
    for (mo, dy), col in sorted(targets.items()):
        date_str = datetime(year, mo, dy).strftime("%Y-%m-%d")
        counts = daily.get(date_str, Counter())
        updates, cleared = {}, 0
        for rid, sku, fields in stock_rows:
            want = counts.get(sku)
            cur = str(fields.get(col) or "").strip()
            if want:
                if cur != str(want):
                    updates[rid] = {col: str(want)}
            elif cur:
                # 无单即清空：不只清旧的 "0"，也清残留的旧非零值（订单改期/取消后重跑）
                updates[rid] = {col: None}
                cleared += 1
        items = list(updates.items())
        for i in range(0, len(items), 200):
            _lark(["+record-batch-update", "--base-token", base_token,
                   "--table-id", table_id],
                  payload={"update_records": dict(items[i:i + 200])})
            time.sleep(0.4)
        print(f"  [完成] {col}: 有单 {len(counts)} 款（{sum(counts.values())} 件）"
              f"→ 写入/更新 {len(items) - cleared} 条，清空旧0 {cleared} 条")

    # 总新增订单量列：各日期列之和（与日列同口径：0 值清空、留空不动）
    total_col = "总新增订单量"
    if not _field_id_by_name(base_token, table_id, total_col):
        _lark(["+field-create", "--base-token", base_token, "--table-id", table_id,
               "--as", "user"], payload={"name": total_col, "type": "text"})
        print(f"  [建列] {total_col}")
    updates, cleared, ok_total = {}, 0, 0
    for rid, sku, fields in stock_rows:
        want = sum(daily.get(d, Counter()).get(sku, 0) for d, _ in rng)
        cur = str(fields.get(total_col) or "").strip()
        if want:
            if cur != str(want):
                updates[rid] = {total_col: str(want)}
            else:
                ok_total += 1
        elif cur:
            # 无单即清空：旧值可能来自更早区间（如已删的 begin 之前日期列）
            updates[rid] = {total_col: None}
            cleared += 1
    items = list(updates.items())
    for i in range(0, len(items), 200):
        _lark(["+record-batch-update", "--base-token", base_token,
               "--table-id", table_id],
              payload={"update_records": dict(items[i:i + 200])})
    print(f"  [完成] {total_col}: 已更新 {len(items)} 条（一致 {ok_total} 条 / 清空 {cleared} 条）")
    print("\n[完成] 全部日期列处理完毕")
    return 0


def run(args):
    common.ensure_utf8_stdout()
    cred = mabang._mabang_cred()
    url = args.url or credentials.get().feishu_base_url()
    if not url:
        print("[错误] 未提供 --url 且配置 feishu.base_url 缺失")
        return 1
    base_token = resolve_base(url)
    table_id = resolve_table(base_token, args.table)
    print(f"[表格] base_token={base_token} table_id={table_id}（{args.table}）")

    print("\n[拉取] 马帮库存 SKU 列表（stock.getStockList，aamz 域）...")
    stocks = fetch_stock_list(cred)
    neg = [s for s in stocks if isinstance(s["stockQuantity"], (int, float)) and s["stockQuantity"] < 0]
    print(f"[数据] 共 {len(stocks)} 条库存 SKU；负库存 {len(neg)} 条")
    for s in stocks[:10]:
        print(f"  {s['stockSku']} | {s['nameCN']} | 库存={s['stockQuantity']} | {s['statusText']}")
    if len(stocks) > 10:
        print(f"  ... 其余 {len(stocks) - 10} 条")

    if not args.apply:
        print("\n（dry-run 未写入；确认无误后加 --apply 全量重建）")
        return 0

    # 1) 清空现有记录
    old_ids = _read_all_record_ids(base_token, table_id)
    print(f"\n[清空] 删除现有记录 {len(old_ids)} 条...")
    for i in range(0, len(old_ids), 100):
        cmd = ["+record-delete", "--base-token", base_token, "--table-id", table_id, "--yes"]
        for rid in old_ids[i:i + 100]:
            cmd += ["--record-id", rid]
        _lark(cmd)
        time.sleep(0.3)

    # 2) 写入新记录
    print(f"[写入] 创建 {len(stocks)} 条记录...")
    pairs = _create_records(base_token, table_id, stocks)
    print(f"  创建成功 {len(pairs)} 条")

    # 3) 图片附件（并行下载，串行上传）
    with_pic = [p for p in pairs if p[1]["stockPicture"]]
    print(f"[图片] 需上传 {len(with_pic)} 张（并行下载，串行上传）...")
    tmpdir = tempfile.mkdtemp(prefix="mbstock_")
    paths = {}

    def _dl(pair):
        rid, it = pair
        try:
            r = requests.get(it["stockPicture"], timeout=30)
            r.raise_for_status()
            path = os.path.join(tmpdir, f"{rid}.jpg")
            with open(path, "wb") as f:
                f.write(r.content)
            return rid, path
        except Exception:
            return rid, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for rid, path in ex.map(_dl, with_pic):
            if path:
                paths[rid] = path

    ok_img, fail_img = 0, []
    for rid, it in pairs:
        path = paths.get(rid)
        if not path:
            if it["stockPicture"]:
                fail_img.append(it["stockSku"])
            continue
        try:
            _lark(["+record-upload-attachment", "--base-token", base_token,
                   "--table-id", table_id, "--record-id", rid,
                   "--field-id", "图", "--file", path])
            ok_img += 1
        except Exception as e:
            fail_img.append(f"{it['stockSku']}({str(e)[:40]})")
        time.sleep(0.2)
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n[完成] 全量重建：{len(pairs)} 条记录 | 图片成功 {ok_img} / 失败 {len(fail_img)}")
    if fail_img:
        print("  图片失败:", "、".join(map(str, fail_img[:8])))
    print("[汇总] 负库存 " + str(len(neg)) + " 条: " +
          ("、".join(f"{s['stockSku']}({s['stockQuantity']})" for s in neg[:10]) if neg else "无"))
    return 0

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
from datetime import date, datetime, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

from wb_ops import common
from wb_ops import credentials
from wb_ops.services.order import mabang
from .feishu_register import _lark, resolve_base, resolve_table

STOCK_LIST_URL = "https://aamz.mabangerp.com/index.php"

DATE_FMT = "%Y-%m-%d"
# 图片附件列候选名（本表实际列名为「图」，其余为兼容）
PIC_FIELD_CANDIDATES = ("图", "图片", "图片附件")


def _sv(v):
    """单元格值 → 去空格字符串（兼容 list/dict 富文本）"""
    if v is None:
        return ""
    if isinstance(v, list):
        v = "".join(str(x.get("text", x) if isinstance(x, dict) else x) for x in v)
    return str(v).strip()


def _to_num(v):
    """尽力转数值；失败返回 None（兼容 "‑2" 这类字符串库存）"""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _parse_day(s, flag):
    """YYYY-M-D → date（strptime 自动补零规范化）；空串返回 None，非法抛 ValueError"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, DATE_FMT).date()
    except ValueError:
        raise ValueError(f"{flag} 日期格式应为 YYYY-MM-DD，实际收到: {s!r}")


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


def _record_list_all(base_token, table_id):
    """分页读全表 → list[dict]（lark-cli ndjson 单次上限 2000，按 manifest 翻页）"""
    rows, offset, page = [], 0, 0
    while True:
        out = os.path.join(tempfile.gettempdir(), f"_mbstock_rl_{os.getpid()}_{page}.ndjson")
        man_path = out.replace(".ndjson", ".manifest.json")
        for p in (out, man_path):
            if os.path.exists(p):
                os.remove(p)
        man = _lark(["+record-list", "--base-token", base_token, "--table-id", table_id,
                     "--limit", "2000", "--offset", str(offset),
                     "--format", "ndjson", "--output", out, "--overwrite"])
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
        offset, page = int(nxt), page + 1
        time.sleep(0.3)


def _read_all_record_ids(base_token, table_id):
    """读全表现有记录 ID 列表（清空前用；分页防截断）"""
    return [str(d["record_id"]) for d in _record_list_all(base_token, table_id)
            if d.get("record_id")]


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
        chunk_items = items[i:i + 200]
        if len(rid_list) != len(chunk_items):
            raise RuntimeError(
                f"record-batch-create 第 {i // 200 + 1} 批返回 {len(rid_list)}/{len(chunk_items)} 条，"
                f"数量不符已中止（请核对飞书表后重跑 mabang-stock-register --apply）")
        for rid, it in zip(rid_list, chunk_items):
            pairs.append((str(rid), it))
        time.sleep(0.5)
    return pairs


# ---------------- 每日新订单量列 ----------------

def _field_id_by_name(base_token, table_id, name):
    """field-list 查列 id（走 fr._lark，二进制解析已验证）。
    查询失败直接抛出 —— 不再把「查询失败」当「列不存在」，否则会误建同名列
    （飞书报 800010205 unique field name，且发生在数百条写入之后）。"""
    d = _lark(["+field-list", "--base-token", base_token,
               "--table-id", table_id])
    # fr._lark 已剥壳：字段列表直接在顶层 d["fields"]
    for f in d.get("fields") or []:
        if f.get("name") == name:
            return f.get("id")
    return None


def _resolve_attachment_field(base_token, table_id):
    """定位附件列的真实列名：候选名优先，其次退到任意 attachment 类型列；都没有返回 None"""
    d = _lark(["+field-list", "--base-token", base_token, "--table-id", table_id])
    fields = d.get("fields") or []
    by_name = {f.get("name"): f for f in fields}
    for nm in PIC_FIELD_CANDIDATES:
        if nm in by_name:
            return nm
    for f in fields:
        if f.get("type") == "attachment":
            return f.get("name")
    return None


def read_orders_daily(base_token, orders_table_id, begin, end):
    """读「订单登记」→ {date: {库存SKU: 总件数}}；无库存SKU 的订单单独计数"""
    daily, no_sku = {}, Counter()
    for d in _record_list_all(base_token, orders_table_id):
        fields = d.get("fields") or d
        dt = _sv(fields.get("日期"))[:10]
        if not dt or dt < begin or dt > end:
            continue
        sku = _sv(fields.get("库存SKU"))
        try:
            qty = int(float(_sv(fields.get("订单量")) or 1))
        except ValueError:
            qty = 1
        if not sku:
            no_sku[dt] += qty
            continue
        daily.setdefault(dt, Counter())[sku] += qty
    return daily, no_sku


def read_stock_records(base_token, table_id):
    """读「马帮库存登记表」→ [(record_id, 库存SKU, fields_dict)]"""
    res = []
    for d in _record_list_all(base_token, table_id):
        rid = d.get("record_id")
        if rid:
            fields = d.get("fields") or d
            res.append((str(rid), _sv(fields.get("库存SKU")), fields))
    return res


DATE_COL_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日新订单量$")


def _list_date_columns(base_token, table_id, ref_date):
    """field-list → {(year, month, day): 列名}。列名不带年份，故按 ref_date 推断：
    月日 ≤ ref_date 视为同年，否则视为上一年（约定表内不预建未来列）。"""
    d = _lark(["+field-list", "--base-token", base_token, "--table-id", table_id])
    rm, rd = ref_date.month, ref_date.day
    cols = {}
    for f in d.get("fields") or []:
        m = DATE_COL_RE.match(f.get("name") or "")
        if m:
            mo, dy = int(m.group(1)), int(m.group(2))
            y = ref_date.year if (mo, dy) <= (rm, rd) else ref_date.year - 1
            cols[(y, mo, dy)] = f["name"]
    return cols


def _date_range(begin_d, end_d):
    """[begin, end] 闭区间 → [date, ...]"""
    out, cur = [], begin_d
    while cur <= end_d:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def run_daily(args):
    """马帮库存登记表日期列管理：
    默认（不带 --begin/--date/--end）：不删旧列、只建今天列（缺失时）、更新全部已有日期列数据；
    显式 --begin（或 --date）：删除早于该日的日期列 + 补建区间缺列 + 填充；
      --end 必须与 --begin/--date 同用（单独使用直接报错，避免静默忽略）。
    填充口径：有订单的 SKU 写数量；无订单的 SKU 留空（重跑时清空残留旧值，含旧 "0" 与旧非零）；
      总新增订单量 = 表内所有存活日期列之和（与 --begin/默认模式无关），同口径清空。"""
    common.ensure_utf8_stdout()
    url = args.url or credentials.get().feishu_base_url()
    if not url:
        print("[错误] 未提供 --url 且配置 feishu.base_url 缺失")
        return 1
    base_token = resolve_base(url)
    table_id = resolve_table(base_token, args.table)
    orders_tid = resolve_table(base_token, args.orders_table)

    today_d = datetime.strptime(time.strftime(DATE_FMT), DATE_FMT).date()
    today = today_d.strftime(DATE_FMT)
    try:
        d_date = _parse_day(args.date, "--date")
        d_begin = _parse_day(args.begin, "--begin")
        d_end = _parse_day(args.end, "--end")
    except ValueError as e:
        print(f"[错误] {e}")
        return 1
    if d_end and not (d_begin or d_date):
        print("[错误] --end 必须与 --begin 或 --date 同用（单独 --end 不触发删列，已中止）")
        return 1
    explicit = bool(d_begin or d_date)
    begin_d = d_begin or d_date or today_d
    end_d = d_end or d_date or today_d
    if begin_d > end_d:
        begin_d, end_d = end_d, begin_d
    begin = begin_d.strftime(DATE_FMT)
    print(f"[表格] 库存表={table_id} 订单表={orders_tid}")
    print(f"[日期] {begin} ~ {end_d.strftime(DATE_FMT)}（explicit={explicit}）")

    stock_rows = read_stock_records(base_token, table_id)
    print(f"[库存] 马帮库存登记表 {len(stock_rows)} 条记录")

    # 列信息（含总列预检）——放在任何破坏性写入之前；失败直接中止，不误判为「列不存在」
    total_col = "总新增订单量"
    try:
        existing = _list_date_columns(base_token, table_id, today_d)
        total_fid = _field_id_by_name(base_token, table_id, total_col)
    except Exception as e:
        print(f"[错误] 读取「{args.table}」列信息失败（尚未做任何写入，可直接重试）: {str(e)[:200]}")
        return 1

    # 删除清单：仅显式 --begin/--date 时删除 begin 之前的日期列
    del_cols = []
    if explicit:
        del_cols = [nm for (y, mo, dy), nm in sorted(existing.items())
                    if date(y, mo, dy) < begin_d]

    # 建列清单：显式=begin~end 区间缺列；默认=仅今天缺列
    rng = _date_range(begin_d, end_d)
    if not explicit:
        rng = [today_d]

    if not args.apply:
        missing = [f"{d_.month}月{d_.day}日新订单量" for d_ in rng
                   if (d_.year, d_.month, d_.day) not in existing]
        survivors = {k: v for k, v in existing.items() if v not in del_cols}
        for d_ in rng:
            survivors[(d_.year, d_.month, d_.day)] = f"{d_.month}月{d_.day}日新订单量"
        upd_dates = sorted(f"{k[1]}月{k[2]}日" for k in survivors)
        print(f"[dry-run] 将删除旧列 {len(del_cols)} 个: {del_cols or '无'}")
        print(f"[dry-run] 将新建列 {len(missing)} 个: {missing or '无'}")
        print(f"[dry-run] 将更新数据列 {len(upd_dates)} 个（=全部存活列）: {upd_dates or '无'}")
        print(f"[dry-run] 总列「{total_col}」：{'已存在' if total_fid else '不存在（将新建）'}")
        print("（dry-run 未写入；确认无误后加 --apply 执行）")
        return 0

    for nm in del_cols:
        _lark(["+field-delete", "--base-token", base_token, "--table-id", table_id,
               "--field-id", nm, "--yes"])
        print(f"  [删列] {nm}")
    if del_cols:
        time.sleep(0.5)
        existing = _list_date_columns(base_token, table_id, today_d)

    for d_ in rng:
        key = (d_.year, d_.month, d_.day)
        if key not in existing:
            col = f"{d_.month}月{d_.day}日新订单量"
            _lark(["+field-create", "--base-token", base_token, "--table-id", table_id,
                   "--as", "user"], payload={"name": col, "type": "text"})
            print(f"  [建列] {col}")
            time.sleep(0.3)
            existing[key] = col

    if not total_fid:
        _lark(["+field-create", "--base-token", base_token, "--table-id", table_id,
               "--as", "user"], payload={"name": total_col, "type": "text"})
        print(f"  [建列] {total_col}")

    # 统计区间：覆盖所有存活日期列 + 目标列 + today（保证已有列数据完整）
    all_dates = [date(y, mo, dy) for (y, mo, dy) in existing] + list(rng)
    stats_begin = min(all_dates + [begin_d, today_d]).strftime(DATE_FMT)
    daily, no_sku = read_orders_daily(base_token, orders_tid, stats_begin, today)
    print(f"[订单] 统计区间 {stats_begin}~{today}："
          f"{sum(sum(c.values()) for c in daily.values())} 件；"
          f"无库存SKU 未归属: " +
          ("、".join(f"{k}={v}" for k, v in sorted(no_sku.items())) if no_sku else "无"))

    # 更新/填充：目标列 = rng 列 + 存活的所有已存在日期列
    targets = {(y, mo, dy): col for (y, mo, dy), col in existing.items()}
    for d_ in rng:
        targets[(d_.year, d_.month, d_.day)] = f"{d_.month}月{d_.day}日新订单量"
    for key, col in sorted(targets.items()):
        date_str = date(*key).strftime(DATE_FMT)
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
              f"→ 写入/更新 {len(items) - cleared} 条 · 清空 {cleared} 条 · "
              f"一致 {len(stock_rows) - len(items)} 条")

    # 总新增订单量列：表内所有存活日期列之和（与 --begin/默认模式无关）
    total_dates = sorted(date(y, mo, dy).strftime(DATE_FMT) for (y, mo, dy) in existing)
    updates, cleared, ok_total = {}, 0, 0
    for rid, sku, fields in stock_rows:
        want = sum(daily.get(d, Counter()).get(sku, 0) for d in total_dates)
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
    print(f"  [完成] {total_col}: 写入/更新 {len(items) - cleared} 条 · 清空 {cleared} 条 · "
          f"一致 {ok_total} 条（日期列 {len(total_dates)} 个）")
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

    # 附件列预检（放在任何破坏性写入之前；字段查询失败不静默）
    try:
        pic_field = _resolve_attachment_field(base_token, table_id)
    except Exception as e:
        print(f"[错误] 读取「{args.table}」列信息失败（未改动任何记录）: {str(e)[:200]}")
        return 1
    if not pic_field:
        print(f"[错误]「{args.table}」未找到图片附件列（候选：{'/'.join(PIC_FIELD_CANDIDATES)}），"
              f"已中止，未改动任何记录。")
        return 1
    print(f"[图片列] {pic_field}")

    print("\n[拉取] 马帮库存 SKU 列表（stock.getStockList，aamz 域）...")
    stocks = fetch_stock_list(cred)
    neg = [s for s in stocks if (_to_num(s["stockQuantity"]) or 0) < 0]
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
    if len(pairs) != len(stocks):
        print(f"[错误] 创建记录 {len(pairs)}/{len(stocks)} 条，数量不符已中止（请重跑本命令）")
        return 1
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
                   "--field-id", pic_field, "--file", path])
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

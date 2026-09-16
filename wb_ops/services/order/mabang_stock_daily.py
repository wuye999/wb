# -*- coding: utf-8 -*-
"""
wb_ops 马帮库存登记表每日订单列管理 (mabang_stock_daily)
管理「马帮库存登记表」历史日期列维护、新建今日/区间日期列、统计各 SKU 订单增量并汇总总新增订单量。
"""
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta

from wb_ops import common
from wb_ops import credentials
from .feishu_register import _lark, resolve_base, resolve_table

DATE_COL_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日新订单量$")


def read_orders_daily(base_token, orders_table_id, begin, end):
    """读「订单登记」→ {date: {库存SKU: 总件数}}；无库存SKU 的订单单独计数"""
    from .mabang_stock import _record_list_all, _sv
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
    from .mabang_stock import _record_list_all, _sv
    res = []
    for d in _record_list_all(base_token, table_id):
        rid = d.get("record_id")
        if rid:
            fields = d.get("fields") or d
            res.append((str(rid), _sv(fields.get("库存SKU")), fields))
    return res


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
    from .mabang_stock import DATE_FMT, _parse_day, _field_id_by_name

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

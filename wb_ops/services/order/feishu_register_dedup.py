# -*- coding: utf-8 -*-
"""
wb_ops 飞书「订单登记」存量重复清理（wb.py feishu-register-dedup）

背景（2026-10 事故）：
  `feishu_register.fetch_existing_order_ids` 旧实现只读 lark-cli ndjson 第一页
  （默认上限 2000 条、按插入序最早在前、无 has_more 翻页）→「订单登记」表记录数
  越过 2000 行（约 2026-10-01）后去重集被冻结，之后每次 mabang-process / feishu-register
  都把已登记订单判为「未登记」而重复写入。实测表内 6429 行仅 3496 个唯一订单编号。
  读取侧已修复（见 feishu_register.scan_order_ids）；本命令负责清理**已产生的存量重复**。

做法：
  1. 全表分页读「订单编号」+「日期」（`feishu_register._record_list_all`，防 2000 截断）
  2. 按「订单编号」分组；每组**保留表内出现的第一条**（= 最早登记的那条，读取即插入顺序），
     其余全部进入删除清单
  3. `+record-delete --yes` 逐条删（≤100/批）；默认 **dry-run**，`--apply` 才真正删除

约定：
  - 「订单编号」为空的行**不动**（单独计数，属待人工核对数据），不计入删/留
  - 天然幂等，可安全续跑：每次运行都实时重算分组，已删行不再出现；删到一半失败直接重跑
  - dry-run 阶段即产出完整「保留/删除」CSV 报告（apply 前的可追溯备份清单）
"""
import csv
import os
import time

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from .feishu_register import (_lark, _record_list_all, _flat_cell,
                              resolve_base, resolve_table)

ORDER_ID_FIELD = "订单编号"
DATE_FIELD = "日期"
DELETE_BATCH = 100            # +record-delete 单次最多带 100 个 --record-id（照 mabang_stock 先例）
CSV_FIELDS = ["订单编号", "日期", "record_id", "动作", "组内序号"]


def read_order_records(base_token, table_id):
    """全表分页读「订单编号」+「日期」→ ([(record_id, order_id, date)], empty_count)。

    empty_count = 订单编号为空的行数（不动，单独统计）。
    """
    recs, empty = [], 0
    for row in _record_list_all(base_token, table_id,
                                field_ids=[ORDER_ID_FIELD, DATE_FIELD]):
        rid = str(row.get("record_id") or "")
        f = row.get("fields") or row
        oid, d = _flat_cell(f.get(ORDER_ID_FIELD)), _flat_cell(f.get(DATE_FIELD))
        if not oid:
            empty += 1
            continue
        recs.append((rid, oid, d))
    return recs, empty


def build_delete_plan(recs):
    """按订单编号分组 → 保留每组表内第一条，其余删除。

    返回 (groups, to_delete)：
      groups    = [{"order_id", "keep_rid", "keep_date", "rows":[(rid,date)...]}]  仅 >1 行的组
      to_delete = [(rid, order_id, date)]  按表内顺序排列的待删除行
    """
    groups = {}
    for rid, oid, d in recs:
        groups.setdefault(oid, []).append((rid, d))
    plan, to_delete = [], []
    for oid, rows in groups.items():
        if len(rows) > 1:
            plan.append({"order_id": oid, "keep_rid": rows[0][0],
                         "keep_date": rows[0][1], "rows": rows})
            to_delete += [(rid, oid, d) for rid, d in rows[1:]]
    return plan, to_delete


def delete_records(base_token, table_id, rids):
    """+record-delete --yes，≤100/批串行；返回删除行数。异常不吞（打印进度后原样抛出）。"""
    done = 0
    for i in range(0, len(rids), DELETE_BATCH):
        chunk = rids[i:i + DELETE_BATCH]
        cmd = ["+record-delete", "--base-token", base_token, "--table-id", table_id, "--yes"]
        for rid in chunk:
            cmd += ["--record-id", rid]
        try:
            _lark(cmd)
        except Exception as e:
            print(f"\n[错误] 已删除 {done} 行；第 {i // DELETE_BATCH + 1} 批失败: {str(e)[:200]}")
            print("      删除为幂等操作（每次运行实时重算分组），修复后直接重跑本命令即可续删。")
            raise
        done += len(chunk)
        print(f"  [删除] 第 {i // DELETE_BATCH + 1} 批：{len(chunk)} 行（累计 {done}）")
        time.sleep(0.3)
    return done


def write_report(groups, to_delete):
    """保留/删除明细 → data/logs/飞书订单登记去重_{ts}.csv（utf-8-sig）"""
    def _w(f):
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for g in groups:
            w.writerow({"订单编号": g["order_id"], "日期": g["keep_date"],
                        "record_id": g["keep_rid"], "动作": "保留", "组内序号": 1})
            for i, (rid, d) in enumerate(g["rows"][1:], 2):
                w.writerow({"订单编号": g["order_id"], "日期": d, "record_id": rid,
                            "动作": "删除", "组内序号": i})

    return common.write_csv_preferred(config.LOG_DIR, "飞书订单登记去重_{ts}.csv",
                                      "feishu_order_register_dedup_{ts}.csv",
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

    print("[读取] 全表分页读取（limit=2000/页，按 manifest 翻页）...")
    recs, empty = read_order_records(base_token, table_id)
    groups, to_delete = build_delete_plan(recs)
    total = len(recs) + empty

    print(f"[统计] 表内记录 {total} 行 ｜ 订单编号非空 {len(recs)} 行 ｜ "
          f"唯一编号 {len(recs) - sum(len(g['rows']) - 1 for g in groups)} 个 ｜ "
          f"空编号 {empty} 行（不动）")
    print(f"[重复] 重复编号 {len(groups)} 个，多余重复行 {len(to_delete)} 行（待删除）")
    for g in sorted(groups, key=lambda x: -len(x["rows"]))[:5]:
        print(f"  {g['order_id']} × {len(g['rows'])} 行（保留 {g['keep_rid']} @ {g['keep_date'] or '-'}）")
    if len(groups) > 5:
        print(f"  ... 其余 {len(groups) - 5} 个重复编号见 CSV 报告")

    if not to_delete:
        print("\n[完成] 无重复记录，无需清理")
        return 0

    path = write_report(groups, to_delete)
    print(f"[汇总] 报告: {path}")

    if not args.apply:
        print(f"\n[dry-run] 未删除任何记录；将删除 {len(to_delete)} 行、"
              f"保留 {total - len(to_delete)} 行。确认无误后加 --apply")
        return 0

    rids = [r[0] for r in to_delete]
    limit = getattr(args, "limit", 0) or 0
    if limit and len(rids) > limit:
        print(f"\n[限量] --limit {limit}：本次只删前 {limit} 行（其余下次重跑续删）")
        rids = rids[:limit]
    print(f"\n[写入] 开始删除 {len(rids)} 行（≤{DELETE_BATCH}/批）...")
    n = delete_records(base_token, table_id, rids)
    print(f"\n[完成] 共删除 {n} 行；预期剩余 {total - n} 行")
    print("[复核] 请再跑一次 `python wb.py feishu-register-dedup`（dry-run）确认「多余重复行 0」")
    return 0

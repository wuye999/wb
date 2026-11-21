"""商品操作逐项回执；各通道使用相同表头、编码、锁和归档规则。"""
import csv
from collections import Counter
from pathlib import Path
import glob
import os
import shutil
from datetime import datetime
from sellerops.core.files import FileLock
from sellerops.settings.paths import DataPaths

ARCHIVE_MAX_MB = 50


def _normalize_csv_encoding(data):
    if not os.path.exists(data.result_csv) or os.path.getsize(data.result_csv) == 0:
        return
    try:
        with open(data.result_csv, "r", encoding="utf-8-sig") as f:
            f.read()
        return
    except UnicodeDecodeError:
        pass
    raw = open(data.result_csv, "rb").read().splitlines()
    lines = []
    for ln in raw:
        try:
            lines.append(ln.decode("utf-8-sig"))
        except UnicodeDecodeError:
            lines.append(ln.decode("gb18030"))
    os.makedirs(os.path.dirname(data.result_csv), exist_ok=True)
    shutil.copy2(data.result_csv, data.result_csv + ".bak")
    with open(data.result_csv, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"  ⚠ ops_result.csv 非 UTF-8 编码（可能被 Excel 另存过），已备份 {data.result_csv}.bak 并转码为 UTF-8-SIG")


def _rotate_result_csv_if_needed(data):
    """按「月」归档 ops_result.csv，避免追加写无限增长（实测 2026-09 单月已达 7 万行 / 4.7MB）。

    规则：
      ① 现有文件最后修改时间不在当前月份 → 移入 `data/logs/archive/ops_result_YYYY-MM.csv`；
      ② 同月文件超过 ARCHIVE_MAX_MB 时 → 移入 `.../ops_result_YYYY-MM-partN.csv`。
    只「移动」不删除，归档文件仍可直接用 Excel 打开，便于追溯；归档动作会打印一行提示。
    """
    if not os.path.exists(data.result_csv) or os.path.getsize(data.result_csv) == 0:
        return
    mtime = datetime.fromtimestamp(os.path.getmtime(data.result_csv))
    now = datetime.now()
    month_tag = mtime.strftime("%Y-%m")
    oversized = os.path.getsize(data.result_csv) > ARCHIVE_MAX_MB * 1024 * 1024
    if mtime.strftime("%Y-%m") == now.strftime("%Y-%m") and not oversized:
        return
    os.makedirs(data.archive_dir, exist_ok=True)
    base = os.path.join(data.archive_dir, f"ops_result_{month_tag}.csv")
    if oversized:
        base = os.path.join(data.archive_dir, f"ops_result_{month_tag}-part"
                            f"{len(glob.glob(os.path.join(data.archive_dir, f'ops_result_{month_tag}-part*.csv')))+1}.csv")
    dst = base
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = base[:-4] + f"-{n}.csv"
    shutil.move(data.result_csv, dst)
    print(f"  [归档] ops_result.csv（{mtime:%Y-%m-%d} 前写入）已移入 {dst}，本次将新建当次明细文件")


def append_results(data: DataPaths, rows):
    os.makedirs(data.log_dir, exist_ok=True)
    with FileLock(data.result_csv):
        _normalize_csv_encoding(data)
        _rotate_result_csv_if_needed(data)
        need_header = not os.path.exists(data.result_csv) or os.path.getsize(data.result_csv) == 0
        with open(data.result_csv, "a", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            if need_header:
                writer.writerow(["店铺", "ID(nmId/chrtId)", "操作", "目标值", "接口响应", "时间"])
            writer.writerows(rows)


def write_product_receipts(data: DataPaths, plans, rows):
    """保留历史流水，并将本次回执与运营可识别的名称、完整 VC 和店铺配对。"""
    append_results(data, rows)
    lookup = {}
    for plan in plans:
        for item in [*plan.get("items", []), *plan.get("blocked_items", [])]:
            for field in ("vc", "nmID", "chrtId"):
                if (key := item.get(field)) is not None:
                    lookup[plan["shopId"], field, key] = item
    output = Path(data.log_dir) / f"商品操作回执_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    counts = {}
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["店铺ID", "商品中文名", "完整VC", "商品或规格ID", "操作", "目标值", "状态", "平台回执", "时间"])
        for sid, key, action, target, message, ts in rows:
            if message.startswith(("SKIP", "鉴权中止")):
                status = "已阻止"
            elif "结果未知" in message:
                status = "结果未知"
            elif message.startswith("FAIL"):
                status = "失败"
            else:
                status = "已受理"
            counts.setdefault(sid, Counter())[status] += 1
            field = "chrtId" if action in {"stock", "stock_wb"} else "nmID"
            item = lookup.get((sid, field, key)) or lookup.get((sid, "vc", key), {})
            writer.writerow([sid, item.get("cn", ""), item.get("vc", ""), key, action, target, status, message, ts])
    for sid, count in counts.items():
        print(f"店 {sid}：" + "，".join(f"{label} {count[label]}" for label in ("已受理", "已阻止", "失败", "结果未知")))
    print(f"[本次逐项回执] {output}")

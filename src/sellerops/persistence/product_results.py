"""商品操作逐项回执；各通道使用相同表头、编码、锁和归档规则。"""
import csv
from dataclasses import dataclass
import json
from typing import Any
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


STATUS_LABELS = {
    "planned": "可提交", "accepted": "已受理", "failed": "失败",
    "unknown": "结果未知", "blocked": "已阻止", "skipped": "正常跳过",
}


@dataclass(frozen=True)
class ProductReceipt:
    shop_id: int
    item: dict
    action: str
    target: Any
    status: str
    message: str = ""
    task_id: str | None = None

    @property
    def identifier(self) -> Any:
        return self.item.get("chrtId") or self.item.get("nmID") or self.item.get("vc")

    @property
    def detail(self) -> str:
        return " ".join(part for part in (self.message, f"taskId={self.task_id}" if self.task_id else "") if part)


def append_product_history(data: DataPaths, receipts: list[ProductReceipt]) -> str | None:
    """沿用既有操作流水表头。"""
    ts = str(datetime.now())
    rows = []
    for receipt in receipts:
        if receipt.action == "price_review" or receipt.status == "skipped":
            continue
        value = receipt.target.get("price", "") if isinstance(receipt.target, dict) else receipt.target
        prefix = {"blocked": "SKIP:", "failed": "FAIL:"}.get(receipt.status, STATUS_LABELS[receipt.status])
        rows.append([receipt.shop_id, receipt.identifier, receipt.action, value,
                     f"{prefix} {receipt.detail}".strip(), ts])
    if rows:
        append_results(data, rows)
        return data.result_csv
    return None


def write_product_receipts(data: DataPaths, receipts: list[ProductReceipt]) -> Path | None:
    """保存结构化的当次回执。"""
    if not receipts:
        return None
    ts = datetime.now()
    output = Path(data.log_dir) / f"商品操作回执_{ts:%Y%m%d_%H%M%S_%f}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["店铺ID", "商品中文名", "完整VC", "商品或规格ID", "操作", "目标值", "状态", "平台回执", "时间"])
        for receipt in receipts:
            value = receipt.target
            if isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            writer.writerow([receipt.shop_id, receipt.item.get("cn", ""), receipt.item.get("vc", ""),
                             receipt.identifier, receipt.action, value, STATUS_LABELS[receipt.status],
                             receipt.detail, str(ts)])
    print(f"[本次逐项回执] {output}")
    return output

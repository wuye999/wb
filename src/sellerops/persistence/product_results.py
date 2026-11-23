"""商品操作逐项回执。"""
import csv
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from sellerops.settings.paths import DataPaths, daily_log_dir

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


def write_product_receipts(data: DataPaths, receipts: list[ProductReceipt]) -> Path | None:
    """保存结构化的当次回执。"""
    if not receipts:
        return None
    ts = datetime.now()
    output = daily_log_dir(ts, data) / f"商品操作回执_{ts:%Y%m%d_%H%M%S_%f}.csv"
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

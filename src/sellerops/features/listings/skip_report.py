"""商品操作的完整跳过报告；终端摘要与CSV使用同一份明细。"""
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sellerops.settings import paths as config

RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


class SkipTracker:
    def __init__(self) -> None:
        self.items: list[tuple[int, str, str]] = []

    def add(self, sid: int, vc: str, reason: str) -> None:
        self.items.append((sid, vc, reason))

    @property
    def count(self) -> int:
        return len(self.items)

    def report(self, action_label: str, show_detail: bool = True) -> list[tuple[int, str, str]] | None:
        if not self.items:
            return
        folder = Path(config.LOG_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        report = folder / f'商品操作跳过_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.csv'
        with report.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['操作', '店铺', 'vendorCode', '原因'])
            writer.writerows((action_label, sid, vc, reason) for sid, vc, reason in self.items)
        reasons = Counter(x[2] for x in self.items)
        print(f"\n{RED}⚠ [跳过汇总] {action_label}：共 {len(self.items)} 条未操作{RESET}")
        for reason, n in reasons.most_common():
            print(f"  {YELLOW}{reason}{RESET}：{n} 条")
        if show_detail:
            shown = self.items[:30]
            for sid, vc, reason in shown:
                print(f"    [跳过] 店{sid} {vc} → {reason}")
            if len(self.items) > 30:
                print(f"    ... 其余 {len(self.items) - 30} 条见完整报告")
        else:
            print(f"  （明细略；共 {len(self.items)} 条）")
        print(f'  [完整跳过报告] {report}')
        return self.items

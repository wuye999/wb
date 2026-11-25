"""本店建卡查重证据，以及复制和表格导入的跳过明细。"""
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from sellerops.features.products.skip_report import write_skip_csv
from sellerops.features.publishing.publish_records import publish_vc_key
from sellerops.persistence import db


@dataclass(frozen=True)
class PublishSkip:
    code: str
    reason: str
    shop_id: int | None = None
    vc: str = ''
    matched_vc: str = ''
    matched_status: str = ''
    line: int | None = None


class PublishSkipReport:
    def __init__(self, label: str) -> None:
        self.label = label
        self.rows: list[PublishSkip] = []
        self.path: Path | None = None

    def write(self) -> None:
        if not self.rows or self.path is not None:
            return
        self.path = write_skip_csv(
            ['操作', '店铺', 'vendorCode', '原因', '阶段', '表格行号', '原因码', '命中VC', '命中状态'],
            ((self.label, row.shop_id, row.vc, row.reason, '表格校验' if row.shop_id is None else '目标店检查',
              row.line, row.code, row.matched_vc, row.matched_status) for row in self.rows),
        )
        print(f'[跳过汇总] {self.label}，共 {len(self.rows)} 条')
        categories = Counter((row.code, row.shop_id is None) for row in self.rows)
        for (code, table_row), count in sorted(categories.items()):
            suffix = '表格行' if table_row else '目标店商品'
            label = REASONS[code]
            print(f'  {label}：{count} 条{suffix}')
        for row in self.rows[:5]:
            label = f'表格第 {row.line} 行' if row.shop_id is None else f'店{row.shop_id} {row.vc}'
            print(f'[跳过] {label}，{row.reason}')
        print(f'[完整跳过报告] {self.path}')

    def summary(self) -> dict:
        targets = [row for row in self.rows if row.shop_id is not None]
        shops = {}
        for row in targets:
            shops.setdefault(row.shop_id, Counter())[row.code] += 1
        return {'skipped_targets': len(targets), 'skip_reasons': dict(Counter(row.code for row in targets)),
                'per_shop': [{'shop_id': sid, 'skipped_targets': sum(reasons.values()), 'skip_reasons': dict(reasons)}
                             for sid, reasons in sorted(shops.items())],
                'skip_samples': [asdict(row) for row in targets[:5]]}


REASONS = {'table_invalid': '表格字段无效', 'table_duplicate': '表内同源商品或目标 VC 重复',
           'target_exact_vc': '目标店已有完整 VC', 'target_same_source': '目标店已有同源数字码',
           'submitted': '目标店已有提交记录', 'batch_duplicate': '本批同源商品重复',
           'excluded': '已标记非货盘', 'listing_restricted': '目标店限制上架'}


class TargetCoverage:
    def __init__(self, rows: list[dict]) -> None:
        self.exact = {vc: 'trashed' if row.get('trashedAt') else 'active'
                      for row in rows if (vc := (row.get('vendorCode') or '').strip())}
        self.by_source = {}
        for vc in sorted(self.exact, key=lambda code: (self.exact[code] == 'trashed', code)):
            self.by_source.setdefault(publish_vc_key(vc), vc)


def target_skip(sid: int, vc: str, coverage: TargetCoverage | None, submitted_vcs: dict,
                records: dict, restrictions: dict, *, keys=(), duplicate: bool = False) -> PublishSkip | None:
    source = publish_vc_key(vc)
    if coverage:
        matched = vc if vc in coverage.exact else coverage.by_source.get(source)
        if matched:
            code = 'target_exact_vc' if matched == vc else 'target_same_source'
            return PublishSkip(code, f'命中目标店已有同源商品 VC：{matched}', sid, vc, matched,
                               coverage.exact[matched])
    matched = submitted_vcs.get(str(sid), {}).get(source)
    if matched or any(str(sid) in records.get(key, {}) for key in (vc, *keys)):
        return PublishSkip('submitted', REASONS['submitted'], sid, vc, matched or '')
    if duplicate:
        return PublishSkip('batch_duplicate', REASONS['batch_duplicate'], sid, vc)
    if db.listing_blocked(vc, sid, restrictions):
        code = 'excluded' if db.resolve_vc(vc, restrictions)['status'] == 'excluded' else 'listing_restricted'
        return PublishSkip(code, REASONS[code], sid, vc)
    return None

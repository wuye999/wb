"""用标准商品表预览或提交 BCS 建卡。"""
import csv
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from math import isfinite
from pathlib import Path
import re
from zipfile import BadZipFile

from openpyxl import load_workbook

from sellerops.domain.snapshot_values import positive_id
from sellerops.domain.vendor_codes import extract_vc_source_code
from sellerops.features.publishing import card_publish
from sellerops.features.publishing.publish_skips import PublishSkip, PublishSkipReport


def _named_rows(values) -> list[dict]:
    headers = [str(cell or '').strip() for cell in next(values, ())]
    columns = {header for header in headers if header}
    if not columns.intersection({'本店nmId', 'nmId'}):
        raise ValueError('表头缺少本店nmId 或 nmId')
    if len(columns) != sum(bool(header) for header in headers):
        raise ValueError('表格含重复列名')
    return [{name: value for name, value in zip(headers, row) if name} for row in values]


def load_exchange_rows(file_path: str | Path) -> list[dict]:
    """按列名读取标准 XLSX 或 UTF-8 CSV。"""
    path = Path(file_path)
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as file:
            return _named_rows(csv.reader(file))
    if path.suffix.lower() != '.xlsx':
        raise ValueError('只支持 .xlsx 或 .csv 商品表')
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except BadZipFile as exc:
        raise ValueError('无法读取 XLSX 商品表') from exc
    try:
        if '商品列表' not in workbook:
            raise ValueError('工作簿缺少「商品列表」Sheet')
        return _named_rows(workbook['商品列表'].iter_rows(values_only=True))
    finally:
        workbook.close()


def _positive_decimal(value) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
        return number if number.is_finite() and number > 0 else None
    except DecimalException:
        return None


def _price(value, multiplier: Decimal) -> float | None:
    amount = _positive_decimal(value)
    if amount is None:
        return None
    try:
        return float((amount * multiplier).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) or None
    except DecimalException:
        return None


def _dimension(value) -> float | None:
    try:
        number = float(str(value))
        return number if isfinite(number) and number > 0 else None
    except ValueError:
        return None


def build_publish_items(rows, shops: list[int], prefix: str, *, price_multiplier=1.0,
                        price=None, stock: int = 999, include_line: bool = False) -> tuple[list[dict], list[tuple[int, str]]]:
    """返回建卡项与 (表格行号, 跳过原因)，纯转换，无平台调用。"""
    if not re.fullmatch(r'[A-Z]{4}', prefix):
        raise ValueError('--prefix 必须为 4 位大写英文字母')
    if not shops or any(type(sid) is not int or sid <= 0 for sid in shops):
        raise ValueError('--shops 必须为逗号分隔的正整数店铺 ID')
    shops = list(dict.fromkeys(shops))
    if type(stock) is not int or stock < 0:
        raise ValueError('--stock 必须为非负整数')

    multiplier = _positive_decimal(price_multiplier) if price is None else Decimal(1)
    if multiplier is None:
        raise ValueError('--price-multiplier 必须为正有限数')
    override = _price(price, multiplier) if price is not None else None
    if price is not None and override is None:
        raise ValueError('--price 必须为有效售价，四舍五入到分后至少为 0.01')

    dimension_columns = {'length': '长(cm)', 'width': '宽(cm)', 'height': '高(cm)', 'weight': '毛重(kg)'}
    items, skipped = [], []
    seen_sources, seen_vcs = set(), set()
    for line, row in enumerate(rows, 2):
        if not any(value not in (None, '') for value in row.values()):
            continue
        nm_id = positive_id(row.get('本店nmId')) or positive_id(row.get('nmId'))
        amount = override if override is not None else _price(row.get('售价(CNY)'), multiplier)
        dims = {field: _dimension(row.get(column)) for field, column in dimension_columns.items()}
        missing = [column for field, column in dimension_columns.items() if dims[field] is None]
        reasons = ([] if nm_id else ['nmId 缺失或无效']) + ([] if amount else ['售价缺失或无效'])
        if missing:
            reasons.append(f"包装尺寸或毛重缺失或无效（{'、'.join(missing)}）")
        if reasons:
            skipped.append((line, '，'.join(reasons)))
            continue
        original_vc = str(row.get('vendorCode') or '').strip()
        source = str(row.get('源数字码') or '').strip()
        if not re.fullmatch(r'\d{6,}', source):
            source = extract_vc_source_code(original_vc, min_digits=6)
        target_vc = f'BCS-{prefix}-{source or nm_id}'
        source_key = source or original_vc
        if target_vc in seen_vcs or (source_key and source_key in seen_sources):
            skipped.append((line, f'表内同源商品或目标 VC 重复（{target_vc}）'))
            continue
        if source_key:
            seen_sources.add(source_key)
        seen_vcs.add(target_vc)
        item = dict(nm_id=nm_id, vendor_code=target_vc, price=amount, target_shops=shops.copy(), stock=stock, **dims,
                    title=str(row.get('俄文标题') or ''), main_image=str(row.get('主图链接') or ''),
                    cn=str(row.get('商品中文名') or ''))
        if include_line:
            item['extra'] = {'table_line': line}
        items.append(item)
    return items, skipped


def run(args) -> int:
    if args.limit < 0:
        raise ValueError('--limit 必须为非负整数（0 表示不限）')
    items, skipped = build_publish_items(load_exchange_rows(args.file),
                                         [int(sid) for sid in args.shops.split(',')], args.prefix,
                                         price_multiplier=args.price_multiplier, price=args.price, stock=args.stock,
                                         include_line=True)
    report = PublishSkipReport('标准表导入上架')
    for line, reason in skipped:
        code = 'table_duplicate' if reason.startswith('表内同源') else 'table_invalid'
        report.rows.append(PublishSkip(code, reason, line=line))
    print(f'[表格] 有效 {len(items)} 行，跳过 {len(skipped)} 行')
    if not items:
        report.write()
        return 1
    try:
        result = card_publish.publish_cards_simple(items, apply=args.apply, interval=args.interval, limit=args.limit,
                                                   skip_report=report)
    except ValueError:
        report.write()
        raise
    if result.get('csv'):
        print(f"建卡回执 {result['csv']}")
    return int(bool(result['fail']))

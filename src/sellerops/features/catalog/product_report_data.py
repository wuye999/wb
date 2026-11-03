"""运营报告的日期、来源链接、订单与评价归一化；不访问平台。"""
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import urlsplit

from sellerops.features.orders.feishu_vc_stats import parse_row as parse_register_row

TZ = timezone(timedelta(hours=8))
KNOWN_STATUSES = {'created', 'buyout', 'cancel', 'return', 'returndefective'}


def timestamp(value: Any) -> datetime:
    """带偏移时间或飞书毫秒时间转为 UTC+8；无偏移的登记时间按 UTC+8。"""
    if isinstance(value, bool) or value is None or value == '':
        raise ValueError('日期缺失或无效')
    if isinstance(value, (int, float)) or re.fullmatch(r'\d{10,13}', str(value)):
        number = float(value)
        if number > 100000000000:
            number /= 1000
        return datetime.fromtimestamp(number, TZ)
    parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)).astimezone(TZ)


def windows(order_days: int, feedback_days: int, now: datetime | None = None) -> tuple[datetime, datetime, datetime | None]:
    """日期窗口含今天，结束为次日零点（排他）。"""
    if not 1 <= order_days <= 31:
        raise ValueError('--order-days 必须为 1–31，官方订单动态仅支持最近31天')
    if feedback_days < 0:
        raise ValueError('--feedback-days 必须 >= 0（0 为可访问的全部历史）')
    current = now or datetime.now(TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    today = current.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=order_days - 1), today + timedelta(days=1),
            today - timedelta(days=feedback_days - 1) if feedback_days else None)


def positive_id(value: Any) -> str:
    text = str(value or '').strip()
    return text if text.isdigit() and int(text) > 0 else ''


def product_url(nm_id: Any) -> str:
    nm = positive_id(nm_id)
    return f'https://www.wildberries.ru/catalog/{nm}/detail.aspx' if nm else ''


def cell_text(value: Any, *, link: bool = False) -> str:
    if isinstance(value, list):
        return ''.join(cell_text(item, link=link) for item in value)
    if isinstance(value, dict):
        keys = ('link', 'url', 'text', 'value') if link else ('text', 'value', 'name')
        return next((cell_text(value[key], link=link) for key in keys if value.get(key) is not None), '')
    return '' if value is None else str(value).strip()


def auxiliary_register_record(record: Any) -> bool:
    """只有记录ID和辅助公式列的行不构成订单；有任何业务内容时不能跳过。"""
    if not isinstance(record, dict):
        return False
    fields = record.get('fields', record)
    return isinstance(fields, dict) and all(
        key in {'record_id', '商品（链接）'} or not cell_text(value)
        for key, value in fields.items())


def register_row(record: dict[str, Any]) -> dict[str, Any]:
    """保留原飞书一记录一单口径，并补充来源链接；不从 VC 尾号推测商品码。"""
    if not isinstance(record, dict):
        raise ValueError('飞书记录不是对象')
    fields = record.get('fields', record)
    if not isinstance(fields, dict):
        raise ValueError('飞书记录缺少有效 fields')
    row = parse_register_row(fields)
    row['日期'] = timestamp(cell_text(fields.get('日期'))).date().isoformat()
    row['record_id'] = str(record.get('record_id') or '')
    raw_shops = fields.get('店铺')
    row['shops'] = [cell_text(item) for item in raw_shops] if isinstance(raw_shops, list) else [cell_text(raw_shops)]
    row['shops'] = [item for item in row['shops'] if item]
    raw_url = cell_text(fields.get('商品链接'), link=True)
    # lark-cli 的展示值可能是 Markdown 超链接，保留原文并单独解析目标。
    markdown = re.fullmatch(r'\[[^\]]*\]\((https?://[^\s]+)\)', raw_url)
    target_url = markdown.group(1) if markdown else raw_url
    nm = positive_id(cell_text(fields.get('wb编号')))
    notes = []
    raw_qty = cell_text(fields.get('订单量'))
    if raw_qty:
        try:
            qty = float(raw_qty)
            if not qty.is_integer() or qty <= 0:
                raise ValueError('订单量无效')
        except ValueError:
            notes.append('订单量无效，件数按原登记规则兜底1')
            row['qty'] = 1
    url_nm = ''
    if raw_url:
        url = urlsplit(target_url)
        match = re.match(r'^/catalog/(\d+)(?:/|$)', url.path)
        if url.scheme in {'http', 'https'} and url.hostname in {
            'wildberries.ru', 'www.wildberries.ru', 'wildberries.cn', 'www.wildberries.cn',
        } and match:
            url_nm = positive_id(match.group(1))
        else:
            notes.append('来源商品链接格式无法识别')
    if nm and url_nm and nm != url_nm:
        notes.append('wb编号与商品链接冲突')
        row['nm_id'], row['url'] = '', ''
    else:
        row['nm_id'] = nm or url_nm
        row['url'] = target_url if url_nm else product_url(nm)
    row['source_url'] = raw_url
    if not row['url']:
        notes.append('缺少可确认的商品链接')
    if not row['vc']:
        notes.append('缺少VC')
    if not row['shops']:
        notes.append('缺少来源店铺')
    row['notes'] = '；'.join(notes)
    return row


def moscow_warehouse(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """只接受唯一可用的莫斯科仓，不使用 officeId，也不跨店缓存。"""
    matches = [row for row in rows if name and name in str(row.get('name') or '')
               and '成都' not in str(row.get('name') or '') and not row.get('isDeleting')]
    if len(matches) != 1 or matches[0].get('isProcessing') or not positive_id(matches[0].get('id')):
        raise ValueError('无法确认唯一可用的莫斯科仓名称与 id，已中止该店采集')
    return {'id': int(matches[0]['id']), 'name': str(matches[0]['name']).strip()}


def nm_vendor_map(snapshot: list[dict[str, Any]], feedbacks: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in [*snapshot, *feedbacks]:
        details = row.get('productDetails', row)
        if not isinstance(details, dict):
            continue
        nm = positive_id(details.get('nmId') or details.get('nmID'))
        vc = str(details.get('vendorCode') or details.get('supplierArticle') or '').strip()
        if nm and vc:
            if nm in result and result[nm] != vc:
                raise ValueError('同店商品码对应多个 VC，无法可靠归属订单')
            result[nm] = vc
    return result


def order_row(raw: dict[str, Any], sid: int, name: str, mapping: dict[str, str],
              warehouse: dict[str, Any], resolve_cn: Any) -> dict[str, Any]:
    nm = positive_id(raw.get('nmId'))
    if not nm or not raw.get('srid'):
        raise ValueError('订单缺少有效商品码或 srid')
    vc = mapping.get(nm, '')
    return {'shop_id': sid, 'shop_name': name, 'nm_id': nm, 'vc': vc,
            'cn': resolve_cn(vc, '') if vc else '', 'url': product_url(nm),
            'order_id': str(raw['srid']), 'created': timestamp(raw.get('createdAt')).isoformat(),
            'updated': timestamp(raw.get('updatedAt')).isoformat(),
            'status': str(raw.get('status') or '').lower(),
            'cancel_type': str(raw.get('cancelType') or ''),
            'warehouse_id': warehouse['id'], 'warehouse': warehouse['name'],
            'warehouse_report_name': str(raw.get('warehouseName') or ''),
            'price': raw.get('sellerPrice')}


def feedback_row(raw: dict[str, Any], sid: int, name: str, resolve_cn: Any) -> dict[str, Any]:
    details = raw.get('productDetails')
    if not isinstance(details, dict) or not positive_id(details.get('nmId')) or not raw.get('id'):
        raise ValueError('评价缺少有效商品码、商品信息或 ID')
    valuation = raw.get('productValuation')
    if isinstance(valuation, bool) or valuation not in (1, 2, 3, 4, 5):
        raise ValueError('评价星级不是1–5，无法归入评分分布')
    vc = str(details.get('supplierArticle') or '').strip()
    return {'shop_id': sid, 'shop_name': name, 'nm_id': positive_id(details['nmId']),
            'vc': vc, 'cn': resolve_cn(vc, '') if vc else '', 'feedback_id': str(raw['id']),
            'created': timestamp(raw.get('createdDate')).isoformat(), 'stars': int(valuation),
            'text': str(raw.get('text') or ''), 'pros': str(raw.get('pros') or ''),
            'cons': str(raw.get('cons') or ''), 'state': str(raw.get('state') or ''),
            'parent_id': str(raw.get('parentFeedbackId') or ''),
            'child_id': str(raw.get('childFeedbackId') or ''),
            'url': product_url(details['nmId'])}

"""运营报告的日期、来源链接、订单与评价归一化；不访问平台。"""
from datetime import datetime, timedelta, timezone
import re
from typing import Any

TZ = timezone(timedelta(hours=8))
KNOWN_STATUSES = {'created', 'buyout', 'cancel', 'return', 'returndefective'}


def timestamp(value: Any) -> datetime:
    """将时间转换为 UTC+8；无时区偏移的时间按 UTC+8 处理。"""
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


def nm_vendor_map(snapshot: list[dict[str, Any]], feedbacks: list[dict[str, Any]]) -> tuple[dict[str, str], int]:
    """快照优先，评价仅补充快照中未出现的商品码。"""
    def collect(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
        by_nm: dict[str, set[str]] = {}
        for row in rows:
            details = row.get('productDetails', row)
            if not isinstance(details, dict):
                continue
            nm = positive_id(details.get('nmId') or details.get('nmID'))
            if not nm:
                continue
            codes = by_nm.setdefault(nm, set())
            for key in ('vendorCode', 'goodsCode', 'supplierArticle'):
                vc = str(details.get(key) or '').strip()
                if vc:
                    codes.add(vc)
                    break
        return by_nm

    result: dict[str, str] = {}
    conflicts = 0
    by_nm = collect(feedbacks) | collect(snapshot)
    for nm, codes in by_nm.items():
        if len(codes) == 1:
            result[nm] = next(iter(codes))
        elif len(codes) > 1:
            conflicts += 1
    return result, conflicts


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

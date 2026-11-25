"""默认商品筛选：按最长数字段计算风险，按完整VC去重展示。"""
from collections import defaultdict
import re
from typing import Any

from sellerops.domain.product_reports import ProductReport, ProductScreening, ReportIssue
from sellerops.features.reports.product_report_analysis import recent_sales, return_metrics, return_orders


def product_key(vc: str) -> str | None:
    """最长连续ASCII数字段；并列且不同的最长段不猜测，nmID不参与归一。"""
    parts = re.findall(r'[0-9]+', vc)
    longest = {part for part in parts if len(part) == max(map(len, parts))} if parts else set()
    return next(iter(longest)) if len(longest) == 1 and not vc.startswith('(') else None


def _sort(values: Any) -> list[str]:
    return sorted(set(values), key=lambda value: re.sub(r'[0-9]+', lambda m: m[0].zfill(16), value))


def screen_products(report: ProductReport) -> ProductScreening:
    """只筛选已知风险；缺评价/完单分母/快照均显式保留未知，不扩大目标店。"""
    result = ProductScreening()
    if report.catalog is None:
        result.issues.append(ReportIssue('默认筛选', '缺少当前价格表与中文名归属，近期筛选不可用'))
    targets = [shop for shop in report.shops if report.target_shop_ids is None or shop['id'] in report.target_shop_ids]
    groups: dict[str, dict[str, Any]] = {}
    invalid: set[str] = set()

    def group(vc: str) -> dict[str, Any] | None:
        vc = str(vc or '').strip()
        key = product_key(vc)
        if not key:
            if vc:
                invalid.add(vc)
            return None
        return groups.setdefault(key, {'key': key, 'orders': [], 'feedbacks': [], 'sales': {},
                                      'local': defaultdict(set), 'present': set(), 'trash': set(),
                                      'mapped': set(), 'names': set(), 'pending': set()})

    for vc, name in (report.catalog.mapped_names.items() if report.catalog else []):
        item = group(vc)
        if item:
            item['mapped'].add(name)
    unknown_shops = set()
    for shop in targets:
        sid, name = shop['id'], shop['name']
        rows = report.snapshots.get(sid)
        if rows is None or report.coverage.get(sid, {}).get('snapshot') == '部分':
            unknown_shops.add(name)
        for raw in rows or []:
            vc = str(raw.get('vendorCode') or raw.get('goodsCode') or '').strip()
            item = group(vc)
            if not item:
                unknown_shops.add(name)
                continue
            if raw.get('trashedAt'):
                item['trash'].add(name)
                continue
            item['local'][vc].add(name)
            item['present'].add(name)
            if raw.get('cn'):
                item['names'].add(str(raw['cn']).strip())
    target_names = {shop['id']: shop['name'] for shop in targets}
    for vc, submitted in report.publish_records.items():
        item = group(vc)
        if item:
            for sid, name in target_names.items():
                if submitted.get(str(sid)):
                    item['pending'].add(name)
    for kind, rows in [('orders', return_orders(report)), ('feedbacks', report.feedbacks)]:
        for row in rows:
            item = group(row.get('vc', ''))
            if not item:
                continue
            item[kind].append(row)
            if row.get('cn'):
                item['names'].add(str(row['cn']).strip())
    for vc, sale in recent_sales(report).items():
        item = group(vc)
        if item:
            item['names'].update(sale['names'])
            item['sales'][vc] = sale
    orders_complete = report.orders_complete
    incomplete = not orders_complete or any(
        report.coverage.get(shop['id'], {}).get('feedbacks') != '完整' for shop in report.shops)
    for item in groups.values():
        metrics = return_metrics(item['orders'])
        feedbacks = item['feedbacks']
        rating = sum(row['stars'] for row in feedbacks) / len(feedbacks) if feedbacks else None
        low = rating is not None and rating <= 3.5
        high = metrics['completed_compatible_return_rate'] is not None and metrics['completed_compatible_return_rate'] >= 0.3
        names = item['mapped'] or item['names']
        matches = names & report.catalog.price_names if report.catalog else set()
        cn = next(iter(matches)) if len(matches) == 1 else '、'.join(_sort(names))
        shared = {'key': item['key'], 'cn': cn, 'rating': rating, 'feedback_count': len(feedbacks),
                  'completed_compatible_return_rate': metrics['completed_compatible_return_rate'], 'completed': metrics['orders'] - metrics['pending'],
                  'combined_returns': metrics['combined_returns']}
        for vc, shops in item['local'].items():
            row = {**shared, 'vc': vc, 'present_shops': '、'.join(_sort(shops))}
            if low:
                result.low_rating.append(row)
            if high:
                result.high_returns.append(row)
        if low or high or len(matches) != 1 or not item['sales']:
            continue
        absent = set(target_names.values()) - item['present']
        pending = absent & item['pending']
        trash = absent & item['trash']
        unknown = absent & unknown_shops
        missing = absent - pending - trash - unknown
        waiting = ([name + '（快照未知）' for name in _sort(unknown)] +
                   [name + '（已有提交）' for name in _sort(pending - unknown)] +
                   [name + '（回收站）' for name in _sort(trash - unknown - pending)])
        notes = []
        if rating is None:
            notes.append('评分未知')
        if metrics['completed_compatible_return_rate'] is None:
            notes.append('退货率未知')
        if incomplete:
            notes.append('风险来源部分缺失')
        if not orders_complete:
            notes.append('订单采集不完整，出单件数仅为已知部分')
        if not report.publish_records_complete:
            notes.append('提交记录未知')
        for vc, sale in item['sales'].items():
            result.recent.append({**shared, 'vc': vc, 'orders7': sale['orders7'],
                                  'orders14': sale['orders14'], 'source_nms': sale['source_nms'],
                                  'present_shops': '、'.join(_sort(item['present'])),
                                  'missing_shops': '、'.join(_sort(missing)), 'waiting_shops': '、'.join(waiting),
                                  'note': '；'.join(_sort(notes))})
    if invalid:
        result.issues.append(ReportIssue('默认筛选', f'{len(invalid)}个VC没有唯一最长数字段，未参与筛选'))
    result.low_rating.sort(key=lambda row: (row['rating'], row['vc']))
    result.high_returns.sort(key=lambda row: (-row['completed_compatible_return_rate'], row['vc']))
    result.recent.sort(key=lambda row: (-row['orders14'], -row['orders7'], row['vc']))
    return result

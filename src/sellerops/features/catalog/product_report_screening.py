"""默认商品筛选：按最长数字段计算风险，按完整VC去重展示。"""
from collections import defaultdict
from datetime import timedelta
import re
from typing import Any

from sellerops.domain.product_reports import ProductReport, ProductScreening, ReportIssue
from sellerops.features.catalog.product_report_analysis import return_metrics
from sellerops.features.catalog.product_report_data import positive_id, timestamp


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
        return groups.setdefault(key, {'key': key, 'orders': [], 'feedbacks': [], 'sales': [],
                                      'local': defaultdict(set), 'present': set(), 'trash': set(),
                                      'mapped': set(), 'names': set(), 'pending': set()})

    for vc, name in (report.catalog.mapped_names.items() if report.catalog else []):
        item = group(vc)
        if item:
            item['mapped'].add(name)
    unknown_shops = set()
    identities: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
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
            nm = positive_id(raw.get('nmId') or raw.get('nmID'))
            if nm:
                identities[(name, vc)].add((item['key'], nm))
    target_names = {shop['id']: shop['name'] for shop in targets}
    for vc, submitted in report.publish_records.items():
        item = group(vc)
        if item:
            for sid, name in target_names.items():
                if submitted.get(str(sid)):
                    item['pending'].add(name)
    for kind, rows in [('orders', report.orders), ('feedbacks', report.feedbacks), ('sales', report.sales)]:
        for row in rows:
            item = group(row.get('vc', ''))
            if not item:
                continue
            item[kind].append(row)
            if row.get('cn'):
                item['names'].add(str(row['cn']).strip())
            if kind != 'sales' and positive_id(row.get('nm_id')):
                identities[(str(row['shop_name']), row['vc'])].add((item['key'], str(row['nm_id'])))
            elif kind == 'sales' and len(row['shops']) == 1 and positive_id(row.get('nm_id')):
                identities[(str(row['shops'][0]), row['vc'])].add((item['key'], str(row['nm_id'])))
    core_identities: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (name, _), values in identities.items():
        for key, nm in values:
            core_identities[(name, key)].add(nm)
    end = timestamp(report.end).date().isoformat()
    begin7 = (timestamp(end) - timedelta(days=6)).date().isoformat()
    begin14 = (timestamp(end) - timedelta(days=13)).date().isoformat()
    incomplete = bool(report.unavailable_accounts) or any(
        report.coverage.get(shop['id'], {}).get(stage) != '完整'
        for shop in report.shops for stage in ('orders', 'feedbacks'))
    for item in groups.values():
        metrics = return_metrics(item['orders'])
        feedbacks = item['feedbacks']
        rating = sum(row['stars'] for row in feedbacks) / len(feedbacks) if feedbacks else None
        low = rating is not None and rating <= 3.5
        high = metrics['done_rate'] is not None and metrics['done_rate'] >= 0.3
        names = item['mapped'] or item['names']
        matches = names & report.catalog.price_names if report.catalog else set()
        cn = next(iter(matches)) if len(matches) == 1 else '、'.join(_sort(names))
        shared = {'key': item['key'], 'cn': cn, 'rating': rating, 'feedback_count': len(feedbacks),
                  'done_rate': metrics['done_rate'], 'completed': metrics['orders'] - metrics['pending'],
                  'combined_returns': metrics['combined_returns']}
        for vc, shops in item['local'].items():
            row = {**shared, 'vc': vc, 'present_shops': '、'.join(_sort(shops))}
            if low:
                result.low_rating.append(row)
            if high:
                result.high_returns.append(row)
        if low or high or len(matches) != 1:
            continue
        by_vc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sale in item['sales']:
            if begin14 <= sale['日期'] <= end:
                by_vc[sale['vc']].append(sale)
        for vc, sales in by_vc.items():
            nms, notes = set(), set()
            for sale in sales:
                labels = sale['shops']
                name = labels[0] if len(labels) == 1 else '、'.join(labels) or '来源店铺未知'
                if len(labels) != 1:
                    notes.add('来源店铺不唯一')
                nm = positive_id(sale.get('nm_id'))
                if not nm and len(labels) == 1:
                    candidates = {nm for _, nm in identities.get((name, vc), set())}
                    if not candidates:
                        candidates = core_identities.get((name, item['key']), set())
                    if len(candidates) == 1:
                        nm = next(iter(candidates))
                nms.add(name + '：' + (nm or 'nmID未知'))
            absent = {shop['name'] for shop in targets} - item['present']
            pending = absent & item['pending']
            trash = absent & item['trash']
            unknown = absent & unknown_shops
            missing = absent - pending - trash - unknown
            waiting = ([name + '（快照未知）' for name in _sort(unknown)] +
                       [name + '（已有提交）' for name in _sort(pending - unknown)] +
                       [name + '（回收站）' for name in _sort(trash - unknown - pending)])
            if rating is None:
                notes.add('评分未知')
            if metrics['done_rate'] is None:
                notes.add('退货率未知')
            if incomplete:
                notes.add('风险来源部分缺失')
            if not report.feishu_complete:
                notes.add('飞书数据不完整')
            if not report.publish_records_complete:
                notes.add('提交记录未知')
            result.recent.append({**shared, 'vc': vc, 'orders7': sum(s['日期'] >= begin7 for s in sales),
                                  'orders14': len(sales), 'source_nms': '\n'.join(_sort(nms)),
                                  'present_shops': '、'.join(_sort(item['present'])),
                                  'missing_shops': '、'.join(_sort(missing)), 'waiting_shops': '、'.join(waiting),
                                  'note': '；'.join(_sort(notes))})
    if invalid:
        result.issues.append(ReportIssue('默认筛选', f'{len(invalid)}个VC没有唯一最长数字段，未参与筛选'))
    result.low_rating.sort(key=lambda row: (row['rating'], row['vc']))
    result.high_returns.sort(key=lambda row: (-row['done_rate'], row['vc']))
    result.recent.sort(key=lambda row: (-row['orders14'], -row['orders7'], row['vc']))
    return result

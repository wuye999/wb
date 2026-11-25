"""运营报告的纯聚合和本地覆盖比较，不调用任何平台或执行上架。"""
from collections import Counter, defaultdict
from typing import Any

from sellerops.domain.product_reports import ProductReport
from sellerops.features.reports import product_report_data as data


def return_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row['status'] for row in rows)
    orders = len(rows)
    cancel, returned, defective = (counts[key] for key in ('cancel', 'return', 'returndefective'))
    combined = cancel + returned + defective
    pending = counts['created']
    return {'orders': orders, 'cancel': cancel, 'returned': returned, 'defective': defective,
            'combined_returns': combined, 'pending': pending, 'buyout': counts['buyout'],
            'unknown': sum(n for status, n in counts.items() if status not in data.KNOWN_STATUSES),
            'return_rate': combined / orders if orders else None,
            'completed_compatible_return_rate': combined / (orders - pending) if orders > pending else None,
            'actual_rate': (returned + defective) / orders if orders else None,
            'cancel_rate': cancel / orders if orders else None}


def return_orders(report: ProductReport) -> list[dict[str, Any]]:
    # 订单明细的 created 已归一为 UTC+8 ISO 时间。
    return [row for row in report.orders if report.order_begin <= row['created'][:10] <= report.end]


def recent_sales(report: ProductReport) -> dict[str, dict[str, Any]]:
    """按完整 VC 汇总近 7/14 天有效出单，每条订单一件。"""
    begin7, begin14 = report.recent_begin(7), report.recent_begin(14)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in report.orders:
        if row['vc'] and row['status'] in {'created', 'buyout'} and begin14 <= row['created'][:10] <= report.end:
            groups[row['vc']].append(row)

    result = {}
    for vc, rows in groups.items():
        result[vc] = {
            'orders7': sum(row['created'][:10] >= begin7 for row in rows),
            'orders14': len(rows),
            'names': {row['cn'] for row in rows if row['cn']},
            'source_nms': '\n'.join(sorted({f"{row['shop_name']}({row['shop_id']})：{row['nm_id']}" for row in rows})),
            'urls': '\n'.join(sorted({f"{row['shop_name']}({row['shop_id']}) {row['url']}" for row in rows})),
        }
    return result


def _coverage_scope(report: ProductReport, stage: str, complete: str) -> str:
    incomplete = [str(shop['id']) for shop in report.shops
                  if report.coverage.get(shop['id'], {}).get(stage) != '完整']
    reasons = ['部分店铺数据缺失：' + ','.join(incomplete)] if incomplete else []
    if report.unavailable_accounts:
        reasons.append('账号店铺范围未知：' + '、'.join(report.unavailable_accounts))
    return '；'.join(reasons) or complete


def _return_groups(report: ProductReport, orders: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in orders:
        label = row[key] or ('(中文名未知)' if key == 'cn' else f'(VC未知:{row["shop_id"]}:{row["nm_id"]})')
        groups[label].append(row)
    scope = _coverage_scope(report, 'orders', '所选店铺完整')
    result = []
    for label, rows in groups.items():
        result.append({'key': label, 'cn': '、'.join(sorted({row['cn'] for row in rows if row['cn']})),
                       'shops': '、'.join(sorted({row['shop_name'] for row in rows})),
                       'vcs': '\n'.join(sorted({row['vc'] for row in rows if row['vc']})),
                       'urls': '\n'.join(sorted({row['shop_name'] + ' ' + row['url'] for row in rows})),
                       'scope': scope, **return_metrics(rows)})
    return sorted(result, key=lambda row: (-row['orders'], row['key']))


def _link_metrics(report: ProductReport, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: dict[tuple[int, str], dict[str, Any]] = {}
    order_rows: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    feedback_rows: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)

    def add(sid: int, name: str, nm: str, vc: str, cn: str, url: str,
            source: str) -> tuple[int, str]:
        key = (sid, nm or '无商品码:' + vc)
        entry = links.setdefault(key, {'shop_id': sid, 'shop_name': name, 'nm_id': nm,
                                       'vcs': set(), 'cns': set(), 'urls': set(), 'sources': set()})
        entry['vcs'].update([vc] if vc else [])
        entry['cns'].update([cn] if cn else [])
        entry['urls'].update([url] if url else [])
        entry['sources'].add(source)
        return key

    for shop in report.shops:
        sid, name = shop['id'], shop['name']
        for raw in report.snapshots.get(sid) or []:
            nm = data.positive_id(raw.get('nmId') or raw.get('nmID'))
            vc = str(raw.get('vendorCode') or raw.get('goodsCode') or '').strip()
            add(sid, name, nm, vc, str(raw.get('cn') or ''), data.product_url(nm),
                '快照回收站' if raw.get('trashedAt') else '本地在架快照')
    for kind, rows in [('官方订单', report.orders), ('官方评价', report.feedbacks)]:
        for row in rows:
            add(row['shop_id'], row['shop_name'], row['nm_id'], row['vc'], row['cn'], row['url'], kind)
    for rows, grouped in [(orders, order_rows), (report.feedbacks, feedback_rows)]:
        for row in rows:
            grouped[(row['shop_id'], row['nm_id'])].append(row)
    result = []
    for key, entry in links.items():
        sid = entry['shop_id']
        cover = report.coverage.get(sid, {})
        order_state, feedback_state = cover.get('orders', '未知'), cover.get('feedbacks', '未知')
        metrics = return_metrics(order_rows[key])
        if order_state == '未知':
            metrics = dict.fromkeys(metrics)
        fb = feedback_rows[key]
        stars = Counter(row['stars'] for row in fb)
        known = feedback_state != '未知'
        negative = stars[1] + stars[2] if known else None
        result.append({'shop_id': sid, 'shop_name': entry['shop_name'], 'nm_id': entry['nm_id'],
                       'vc': '\n'.join(sorted(entry['vcs'])), 'cn': '、'.join(sorted(entry['cns'])),
                       'url': '\n'.join(sorted(entry['urls'])),
                       'source': '、'.join(sorted(entry['sources'])),
                       'order_scope': order_state, 'feedback_scope': feedback_state,
                       'feedback_count': len(fb) if known else None,
                       'rating': sum(row['stars'] for row in fb) / len(fb) if fb else None,
                       'negative': negative,
                       **{f'star_{i}': stars[i] if known else None for i in range(1, 6)},
                       'note': ('同店商品码存在多个VC，请核对；' if len(entry['vcs']) > 1 else '') +
                               ('风险未知' if not known else '有1–2星差评，供人工判断' if negative else
                                '已取评价内未见1–2星差评；不代表适合上架'), **metrics})
    return sorted(result, key=lambda row: (str(row['shop_id'] or ''), row['shop_name'], row['nm_id'], row['vc']))


def _feedback_groups(report: ProductReport) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in report.feedbacks:
        key = row['vc'] or f'(VC未知:{row["shop_id"]}:{row["nm_id"]})'
        groups[key].append(row)
    scope = _coverage_scope(report, 'feedbacks', '所选店铺可取评价完整')
    result = []
    for key, rows in groups.items():
        stars = Counter(row['stars'] for row in rows)
        result.append({'key': key, 'cn': '、'.join(sorted({row['cn'] for row in rows if row['cn']})),
                       'feedback_count': len(rows), 'negative': stars[1] + stars[2],
                       'rating': sum(row['stars'] for row in rows) / len(rows),
                       **{f'star_{i}': stars[i] for i in range(1, 6)},
                       'shops': '、'.join(sorted({row['shop_name'] for row in rows})),
                       'urls': '\n'.join(sorted({row['shop_name'] + ' ' + row['url'] for row in rows})),
                       'scope': scope + '；不含未授权外部店铺'})
    return sorted(result, key=lambda row: (-row['negative'], -row['feedback_count'], row['key']))


def _sales(report: ProductReport) -> list[dict[str, Any]]:
    negatives = Counter(row['vc'] for row in report.feedbacks if row['vc'] and row['stars'] <= 2)
    sales_scope = _coverage_scope(report, 'orders', '所选店铺完整')
    risk_scope = _coverage_scope(report, 'feedbacks', '所选店铺可取评价完整')
    result = [{'vc': vc, 'cn': '、'.join(sorted(entry['names'])), 'orders7': entry['orders7'],
               'orders14': entry['orders14'], 'source_nms': entry['source_nms'], 'urls': entry['urls'],
               'known_negative': negatives[vc], 'risk_scope': risk_scope, 'sales_scope': sales_scope}
              for vc, entry in recent_sales(report).items()]
    return sorted(result, key=lambda row: (-row['orders14'], row['vc']))


def _gaps(report: ProductReport, sales: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for shop in report.shops:
        sid = shop['id']
        if report.target_shop_ids is not None and sid not in report.target_shop_ids:
            continue
        snapshot = report.snapshots.get(sid)
        active: dict[str, list[str]] = defaultdict(list)
        trashed: set[str] = set()
        for row in snapshot or []:
            vc = str(row.get('vendorCode') or row.get('goodsCode') or '').strip()
            if row.get('trashedAt'):
                trashed.add(vc)
            else:
                active[vc].append(data.positive_id(row.get('nmId') or row.get('nmID')))
        for sale in sales:
            vc = sale['vc']
            submitted = report.publish_records.get(vc, {}).get(str(sid), '')
            if snapshot is None:
                state = '缺少快照无法判断'
            elif vc in active:
                state = '快照中已存在'
            elif vc in trashed:
                state = '快照回收站已有卡，需人工判断'
            elif report.coverage[sid].get('snapshot') == '部分':
                state = '快照不完整无法判断'
            elif submitted:
                state = '已有提交记录待生效或待确认（未回读）'
            elif not report.publish_records_complete:
                state = '提交记录未知，快照中未见'
            else:
                state = '快照中未见'
            result.append({**sale, 'shop_id': sid, 'shop_name': shop['name'], 'state': state,
                           'target_urls': '\n'.join(data.product_url(nm) for nm in active.get(vc, []) if nm),
                           'fetched_at': report.coverage[sid].get('fetched_at', ''),
                           'submitted': str(submitted or '')})
    return result


def analyze(report: ProductReport) -> dict[str, list[dict[str, Any]]]:
    """同一 VC 跨店聚合；风险不作为自动排除条件。"""
    sales = _sales(report)
    orders = return_orders(report)
    return {'by_cn': _return_groups(report, orders, 'cn'), 'by_vc': _return_groups(report, orders, 'vc'),
            'vc_feedback': _feedback_groups(report),
            'links': _link_metrics(report, orders), 'sales': sales, 'gaps': _gaps(report, sales),
            'negative': sorted((row for row in report.feedbacks if row['stars'] <= 2),
                               key=lambda row: (row['stars'], row['created'], row['feedback_id'])),
            'orders': report.orders}

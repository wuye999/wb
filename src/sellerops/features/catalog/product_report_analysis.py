"""运营报告的纯聚合和本地覆盖比较，不调用任何平台或执行上架。"""
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from sellerops.domain.product_reports import ProductReport
from sellerops.features.catalog import product_report_data as data


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
            'done_rate': combined / (orders - pending) if orders > pending else None,
            'actual_rate': (returned + defective) / orders if orders else None,
            'cancel_rate': cancel / orders if orders else None}


def _return_groups(report: ProductReport, key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in report.orders:
        label = row[key] or ('(中文名未知)' if key == 'cn' else f'(VC未知:{row["shop_id"]}:{row["nm_id"]})')
        groups[label].append(row)
    incomplete = [str(shop['id']) for shop in report.shops
                  if report.coverage[shop['id']]['orders'] != '完整']
    scope = '部分店铺数据缺失：' + ','.join(incomplete) if incomplete else '所选店铺完整'
    result = []
    for label, rows in groups.items():
        result.append({'key': label, 'cn': '、'.join(sorted({row['cn'] for row in rows if row['cn']})),
                       'shops': '、'.join(sorted({row['shop_name'] for row in rows})),
                       'vcs': '\n'.join(sorted({row['vc'] for row in rows if row['vc']})),
                       'urls': '\n'.join(sorted({row['shop_name'] + ' ' + row['url'] for row in rows})),
                       'scope': scope, **return_metrics(rows)})
    return sorted(result, key=lambda row: (-row['orders'], row['key']))


def _link_metrics(report: ProductReport) -> list[dict[str, Any]]:
    links: dict[tuple[str, str], dict[str, Any]] = {}
    order_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    feedback_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    def add(sid: int | None, name: str, nm: str, vc: str, cn: str, url: str,
            source: str) -> tuple[str, str]:
        key = (str(sid) if sid is not None else '外部:' + name, nm or '无商品码:' + vc)
        entry = links.setdefault(key, {'shop_id': sid, 'shop_name': name, 'nm_id': nm,
                                       'vcs': set(), 'cns': set(), 'urls': set(), 'sources': set()})
        entry['vcs'].update([vc] if vc else [])
        entry['cns'].update([cn] if cn else [])
        entry['urls'].update([url] if url else [])
        entry['sources'].add(source)
        return key

    names: dict[str, list[int]] = defaultdict(list)
    for shop in report.shops:
        sid, name = shop['id'], shop['name']
        names[name].append(sid)
        names[f'{name}({sid})'].append(sid)
        for raw in report.snapshots.get(sid) or []:
            nm = data.positive_id(raw.get('nmId') or raw.get('nmID'))
            vc = str(raw.get('vendorCode') or raw.get('goodsCode') or '').strip()
            add(sid, name, nm, vc, str(raw.get('cn') or ''), data.product_url(nm),
                '快照回收站' if raw.get('trashedAt') else '本地在架快照')
    for kind, rows, grouped in [('官方订单', report.orders, order_rows),
                                 ('官方评价', report.feedbacks, feedback_rows)]:
        for row in rows:
            key = add(row['shop_id'], row['shop_name'], row['nm_id'], row['vc'], row['cn'], row['url'], kind)
            grouped[key].append(row)
    for row in report.sales:
        labels = row['shops']
        label = '、'.join(labels) or '(来源店铺未知)'
        matches = names.get(label, []) if len(labels) == 1 else []
        sid = matches[0] if len(matches) == 1 else None
        add(sid, label, row['nm_id'], row['vc'], row['cn'], row['url'], '飞书出单记录（历史）')
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
    incomplete = [str(shop['id']) for shop in report.shops
                  if report.coverage[shop['id']]['feedbacks'] != '完整']
    scope = ('所选店铺评价不完整：' + ','.join(incomplete) if incomplete else '所选店铺可取评价完整')
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
    begin7 = (data.timestamp(report.end) - timedelta(days=6)).date().isoformat()
    groups: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(report.sales):
        vc = row['vc']
        key = vc or f'(无VC:{row.get("record_id") or index + 1})'
        entry = groups.setdefault(key, {'vc': vc, 'key': key, 'cns': set(), 'sources': set(),
                                        'links': set(), 'notes': set(), 'orders7': 0, 'qty7': 0,
                                        'orders14': 0, 'qty14': 0})
        entry['orders14'] += 1
        entry['qty14'] += row['qty']
        if row['日期'] >= begin7:
            entry['orders7'] += 1
            entry['qty7'] += row['qty']
        entry['cns'].update([row['cn']] if row['cn'] else [])
        entry['sources'].update(row['shops'])
        entry['links'].add('、'.join(row['shops']) + ' ' + (row['url'] or row['source_url'] or '(无链接)'))
        if row['notes']:
            entry['notes'].add(row['notes'])
    negatives = Counter(row['vc'] for row in report.feedbacks if row['vc'] and row['stars'] <= 2)
    result = []
    for key, entry in groups.items():
        result.append({**{k: v for k, v in entry.items() if not isinstance(v, set)},
                       'cn': '、'.join(sorted(entry['cns'])), 'sources': '、'.join(sorted(entry['sources'])),
                       'urls': '\n'.join(sorted(entry['links'])), 'notes': '；'.join(sorted(entry['notes'])),
                       'known_negative': negatives[entry['vc']],
                       'risk_scope': '仅有已授权店铺可取评价；外部或失败店铺未知，未自动排除',
                       'sales_scope': '完整' if report.feishu_complete else '部分或未知'})
    return sorted(result, key=lambda row: (-row['orders14'], row['key']))


def _gaps(report: ProductReport, sales: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for shop in report.shops:
        sid = shop['id']
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
            if not vc:
                state = '缺VC无法比较'
            elif snapshot is None:
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
    return {'by_cn': _return_groups(report, 'cn'), 'by_vc': _return_groups(report, 'vc'),
            'vc_feedback': _feedback_groups(report),
            'links': _link_metrics(report), 'sales': sales, 'gaps': _gaps(report, sales),
            'negative': sorted((row for row in report.feedbacks if row['stars'] <= 2),
                               key=lambda row: (row['stars'], row['created'], row['feedback_id'])),
            'orders': report.orders}

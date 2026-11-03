"""BCS 店铺访问资料＋WB Open API＋飞书的只读运营报告编排。"""
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import Any

from sellerops.core.errors import AuthenticationError, RateLimitError
from sellerops.domain.product_reports import ProductReport, ReportIssue
from sellerops.features.catalog import product_report_data as data
from sellerops.features.listings.publish_records import load_records
from sellerops.integrations.bcs.capture import BCSSession, MAX_CAPTURE_CHARS, parse_sessions
from sellerops.integrations.bcs.client import BCSClient, OpenAPIShop, client as bcs
from sellerops.integrations.feishu import tables
from sellerops.integrations.wildberries.open_api import WBOpenAPI, OpenAPIResponseError
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.persistence.report_catalog import load_report_catalog
from sellerops.settings import credentials, paths


def _failure(report: ProductReport, source: str, message: str, exc: Exception,
             sid: int | None = None) -> None:
    # 外部异常可能附带响应体/请求头；报告仅记录阶段和异常类别。
    reason = ('认证失败或类别权限不足' if isinstance(exc, AuthenticationError) else
              '接口限流' if isinstance(exc, RateLimitError) else type(exc).__name__)
    if isinstance(exc, OpenAPIResponseError):
        reason = (f'HTTP {exc.status_code}，' if exc.status_code else '') + exc.public_reason
    report.issues.append(ReportIssue(source, f'{message}：{reason}', sid))


def _register(report: ProductReport, end: datetime, url: str, table: str) -> None:
    try:
        address = url or credentials.get().feishu_base_url()
        if not address:
            raise ValueError('缺少飞书地址')
        token = tables.resolve_base(address)
        table_id = tables.resolve_table(token, table)
        raw = tables.list_records(token, table_id)
    except Exception as exc:
        _failure(report, '飞书', '读取订单登记失败，出单候选未知', exc)
        return
    counts: Counter = Counter()
    auxiliary = 0
    seen: set[str] = set()
    begin = (end - timedelta(days=14)).date().isoformat()
    for record in raw:
        if data.auxiliary_register_record(record):
            auxiliary += 1
            continue
        try:
            row = data.register_row(record)
        except (ValueError, TypeError, OverflowError, OSError):
            counts['日期或结构无效，无法确定是否属于窗口'] += 1
            continue
        rid = row['record_id']
        if rid and rid in seen:
            counts['分页重复记录 ID'] += 1
            continue
        if rid:
            seen.add(rid)
        if not begin <= row['日期'] <= report.end:
            continue
        if row['notes']:
            counts[row['notes']] += 1
        report.sales.append(row)
    for reason, count in counts.items():
        report.issues.append(ReportIssue('飞书', f'{reason}：{count}条；原始缺失不能当作零'))
    if auxiliary:
        report.issues.append(ReportIssue('飞书', f'{auxiliary}条仅含记录ID或辅助公式、没有订单信息的占位行未计入',
                                         severity='警告'))
    report.feishu_complete = not counts


def _shop(report: ProductReport, access: Any, start: datetime, end: datetime,
          feedback_start: datetime | None, resolve_cn: Any) -> None:
    sid, name = access.id, access.name
    cover = report.coverage[sid]
    api = None
    try:
        api = WBOpenAPI(access.api_key, report.region)
        warehouse = data.moscow_warehouse(api.warehouses(), paths.DEFAULT_WAREHOUSE_NAME)
        cover.update(warehouse_id=warehouse['id'], warehouse_name=warehouse['name'])
    except Exception as exc:
        _failure(report, '仓库与授权', '未能确认莫斯科仓及授权，停止该店远端采集', exc, sid)
        if api is not None:
            api.close()
        return
    try:
        feedbacks = []
        try:
            feedbacks = api.feedbacks(date_from=int(feedback_start.timestamp()) if feedback_start else None,
                                     date_to=int(end.timestamp()) - 1)
            cover['feedbacks'] = '完整'
        except Exception as exc:
            _failure(report, '评价', '评价采集失败，评价数量未知', exc, sid)
        invalid = 0
        for raw in feedbacks:
            try:
                row = data.feedback_row(raw, sid, name, resolve_cn)
            except (ValueError, TypeError, OverflowError, OSError):
                invalid += 1
                continue
            created = data.timestamp(row['created'])
            if (feedback_start is None or feedback_start <= created) and created < end:
                report.feedbacks.append(row)
        if invalid:
            cover['feedbacks'] = '部分'
            report.issues.append(ReportIssue('评价', f'{invalid}条评价字段无效，数量仅为已知部分', sid))
        try:
            mapping = data.nm_vendor_map(report.snapshots[sid] or [], feedbacks)
        except ValueError:
            mapping = {}
            report.issues.append(ReportIssue('商品映射', '同店商品码与VC冲突，订单VC标为未知', sid))
        try:
            cutoff = min(datetime.now(data.TZ), end)
            fbs = api.fbs_orders(start, cutoff)
            by_rid = {row['rid']: row for row in fbs}
            cover['fbs_orders'] = len(fbs)
            raw_orders = api.orders(start, cutoff)
            cover['orders'] = '完整'
        except Exception as exc:
            _failure(report, '订单', '订单采集失败，退货指标未知', exc, sid)
            return
        counts: Counter = Counter()
        for raw in raw_orders:
            try:
                created_day = data.timestamp(raw.get('createdAt')).date().isoformat()
            except (ValueError, TypeError, OverflowError, OSError):
                counts['订单下单时间无效'] += 1
                continue
            if not report.order_begin <= created_day <= report.end:
                cover['outside_created'] += 1
                continue
            if raw.get('isMp') is False:
                cover['outside_warehouse'] += 1
                continue
            identity = by_rid.get(raw.get('srid'))
            if not identity:
                counts['缺少同店FBS订单的仓库ID关联，未计入'] += 1
                continue
            if int(identity['warehouseId']) != warehouse['id']:
                cover['outside_warehouse'] += 1
                continue
            nm = data.positive_id(raw.get('nmId'))
            if raw.get('isMp') is not True or data.positive_id(identity.get('nmId')) != nm:
                counts['订单动态与FBS订单的商品或仓库类型冲突，未计入'] += 1
                continue
            vc = str(identity.get('article') or mapping.get(nm) or '').strip()
            try:
                row = data.order_row(raw, sid, name, {nm: vc}, warehouse, resolve_cn)
            except (ValueError, TypeError, OverflowError, OSError):
                counts['订单字段无效'] += 1
                continue
            report.orders.append(row)
            if not row['vc']:
                counts['商品码无法映射VC（未猜测VC尾号）'] += 1
            if row['status'] not in data.KNOWN_STATUSES:
                counts['未知订单状态（已单独计数）'] += 1
        for reason, count in counts.items():
            report.issues.append(ReportIssue('订单', f'{reason}：{count}条', sid))
        if counts:
            cover['orders'] = '部分'
    finally:
        api.close()


def _add_shop(report: ProductReport, sid: int, name: str, source: str, *, target: bool) -> None:
    report.shops.append({'id': sid, 'name': name, 'account_source': source,
                         'role': '本机目标店铺' if target else '仅补充统计'})
    report.coverage[sid] = {'orders': '未知', 'feedbacks': '未知',
                            'snapshot': '未知' if target else '不适用：仅补充统计',
                            'fetched_at': '', 'outside_warehouse': 0, 'outside_created': 0}
    report.snapshots[sid] = None
    if target:
        report.target_shop_ids.add(sid)


def _sources(report: ProductReport, wanted: set[int],
             extra_bcs_accounts: Sequence[BCSSession]) -> list[OpenAPIShop]:
    """先选本机目标，再加入临时来源；重复ID不覆盖优先来源或扩大目标范围。"""
    report.target_shop_ids = set()
    selected: dict[int, OpenAPIShop] = {}
    unknown = set(wanted)
    clients = [('本机账号', bcs)] + [
        (f'临时账号{index}', BCSClient(session)) for index, session in enumerate(extra_bcs_accounts, 1)]
    for index, (source, client) in enumerate(clients):
        try:
            accesses = client.fetch_open_api_shops()
            if not accesses:
                raise ValueError('BCS 未返回任何店铺')
        except Exception as exc:
            report.unavailable_accounts.append(source)
            _failure(report, 'BCS/' + source, '店铺列表读取失败，该账号店铺覆盖未知', exc)
            continue
        if index == 0:
            unknown -= {item.id for item in accesses}
            accesses = [item for item in accesses if not wanted or item.id in wanted]
        for access in accesses:
            if access.id in selected:
                if selected[access.id].name != access.name:
                    report.issues.append(ReportIssue('BCS/' + source,
                        '重复店铺ID的名称冲突，保留先前来源，不重复采集', access.id))
                continue
            selected[access.id] = access
            _add_shop(report, access.id, access.name, source, target=index == 0)
    for sid in sorted(unknown):
        report.issues.append(ReportIssue('BCS/本机账号',
            '指定店铺未在本机完整店铺列表中确认，未扩大目标范围', sid))
        if sid not in selected:
            _add_shop(report, sid, f'未确认店铺({sid})', '本机账号（未确认）', target=True)
    return list(selected.values())


def collect(*, shops: str = '', order_days: int = 30, feedback_days: int = 0,
            region: str = 'ru', url: str = '', table: str = '订单登记',
            now: datetime | None = None,
            extra_bcs_accounts: Sequence[BCSSession] = ()) -> ProductReport:
    """临时账号仅补充统计，--shops仅筛本机；所有凭证留在本轮内存中。"""
    start, end, feedback_start = data.windows(order_days, feedback_days, now)
    if region not in {'ru', 'cn'}:
        raise ValueError('--wb-region 必须为 ru 或 cn')
    tokens = [item.strip() for item in shops.split(',') if item.strip()]
    if shops.strip() and (not tokens or any(not data.positive_id(item) for item in tokens)):
        raise ValueError('--shops 应为逗号分隔的正整数店铺ID')
    if any(not isinstance(session, BCSSession) for session in extra_bcs_accounts):
        raise ValueError('临时BCS账号必须为已解析的内存会话')
    wanted = {int(item) for item in tokens}
    generated = now or datetime.now(data.TZ)
    generated = (generated if generated.tzinfo else generated.replace(tzinfo=data.TZ)).astimezone(data.TZ)
    report = ProductReport(generated.isoformat(), start.date().isoformat(),
                           (end - timedelta(days=1)).date().isoformat(),
                           feedback_start.date().isoformat() if feedback_start else '', region)
    accesses = _sources(report, wanted, extra_bcs_accounts)
    try:
        report.catalog = load_report_catalog()
    except Exception as exc:
        _failure(report, '报告名称与价格表', '无法读取当前商品归属与价格表，近期筛选无法确认中文名', exc)
    for access in accesses:
        sid = access.id
        if sid not in report.target_shop_ids:
            continue
        try:
            snapshot = ProductSnapshotRepository.load_snapshot_document(sid)
            if snapshot is None:
                raise FileNotFoundError('缺少快照')
            report.snapshots[sid] = snapshot['rows']
            report.coverage[sid].update(snapshot='可读取', fetched_at=str(snapshot.get('fetchedAt') or '未记录'))
            invalid = sum(not data.positive_id(row.get('nmId') or row.get('nmID')) or
                          not str(row.get('vendorCode') or row.get('goodsCode') or '').strip()
                          for row in snapshot['rows'])
            if invalid:
                report.coverage[sid]['snapshot'] = '部分'
                report.issues.append(ReportIssue('本地快照', f'{invalid}行缺少商品码或VC，未见链接不能判断为缺少', sid))
        except Exception as exc:
            _failure(report, '本地快照', '快照缺失或无效，不能视为空店', exc, sid)
    if report.target_shop_ids:
        try:
            records = load_records()
            if not isinstance(records, dict) or any(not isinstance(value, dict) for value in records.values()):
                raise ValueError('提交记录结构无效')
            targets = {str(sid) for sid in report.target_shop_ids}
            report.publish_records = {vc: {sid: value for sid, value in submitted.items() if sid in targets}
                                      for vc, submitted in records.items()}
            report.publish_records_complete = True
        except Exception as exc:
            _failure(report, '本地提交记录', '无法读取提交记录，未提交状态不能确认', exc)
    try:
        resolve_cn, _ = MappingRepository.build_vc_resolver()
    except Exception as exc:
        resolve_cn = lambda vc, fallback='': fallback
        _failure(report, '中文名映射', '本地中文名映射不可用', exc)
    _register(report, end, url, table)
    for row in report.sales:
        if not row['cn'] and row['vc']:
            row['cn'] = resolve_cn(row['vc'], '')
    for rows in report.snapshots.values():
        for row in rows or []:
            vc = str(row.get('vendorCode') or row.get('goodsCode') or '').strip()
            row['cn'] = resolve_cn(vc, '') if vc else ''
    for access in accesses:
        print(f'[采集] {access.name}({access.id})：官方评价与莫斯科仓订单（只读）', flush=True)
        try:
            _shop(report, access, start, end, feedback_start, resolve_cn)
        except Exception as exc:
            report.coverage[access.id]['orders'] = '部分' if any(
                row['shop_id'] == access.id for row in report.orders) else '未知'
            report.coverage[access.id]['feedbacks'] = '部分' if any(
                row['shop_id'] == access.id for row in report.feedbacks) else '未知'
            _failure(report, '店铺采集', '该店发生未完成的采集错误，继续其他店铺', exc, access.id)
        print(f'[完成] {access.name}({access.id})：订单={report.coverage[access.id]["orders"]}，'
              f'评价={report.coverage[access.id]["feedbacks"]}', flush=True)
    unknown_cn = sum(not row['cn'] for row in [*report.orders, *report.feedbacks])
    if unknown_cn:
        report.issues.append(ReportIssue('中文名映射', f'{unknown_cn}条订单或评价缺少中文名，保留未知分组',
                                         severity='警告'))
    return report


def run(args: Any) -> int:
    """生成完整或注明缺失的本地工作簿；不提供 --apply。"""
    from sellerops.features.catalog.product_report_xlsx import write_report

    output = getattr(args, 'out', '')
    if output and Path(output).suffix.lower() != '.xlsx':
        raise ValueError('--out 必须为 .xlsx 文件')
    extra_bcs_accounts = []
    if getattr(args, 'extra_bcs_stdin', False):
        if sys.stdin.isatty():
            raise ValueError('--extra-bcs-stdin 需要通过标准输入管道传入抓包并关闭输入流')
        try:
            capture = getattr(sys.stdin, 'buffer', sys.stdin).read(MAX_CAPTURE_CHARS + 1)
            if len(capture) > MAX_CAPTURE_CHARS:
                raise ValueError('临时BCS抓包输入超过1MiB上限')
            if isinstance(capture, bytes):
                capture = capture.decode('utf-8-sig')
        except (UnicodeError, OSError):
            raise ValueError('无法读取临时BCS抓包，请通过标准输入传入UTF-8文本') from None
        extra_bcs_accounts = parse_sessions(capture)
    report = collect(shops=args.shops, order_days=args.order_days, feedback_days=args.feedback_days,
                     region=args.wb_region, url=args.url, table=args.table,
                     extra_bcs_accounts=extra_bcs_accounts)
    target = Path(output) if output else Path(paths.LOG_DIR) / (
        '商品运营报告_' + data.timestamp(report.generated_at).strftime('%Y%m%d_%H%M%S') + '.xlsx')
    screening = write_report(report, target, details=getattr(args, 'details', False))
    print(f'[报告] {target.resolve()}')
    print(f'[范围] {len(report.shops)}店，莫斯科仓订单{len(report.orders)}条，'
          f'评价{len(report.feedbacks)}条，飞书近14天记录{len(report.sales)}条')
    print(f'[默认筛选] 低评分{len(screening.low_rating)}个VC，高退货率{len(screening.high_returns)}个VC，'
          f'近期出单候选{len(screening.recent)}个VC；缺口按同数字段判断')
    for issue in [*report.issues, *screening.issues]:
        print(f'[{issue.severity}] {issue.source} 店铺={issue.shop_id or "-"}：{issue.message}')
    return 1 if report.failed or screening.failed else 0

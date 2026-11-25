"""Preview and void the remaining Mabang copies of delivered WB FBS orders."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import partial
from html import unescape
import csv
import re
import time
from uuid import uuid4

import requests

from sellerops.core.errors import SellerOpsError
from sellerops.core.utils import ensure_utf8_stdout, write_csv_preferred
from sellerops.features.orders.matching import VC_RE, load_local_mapping
from sellerops.features.publishing.open_api_common import split_values
from sellerops.integrations.bcs.client import client as bcs
from sellerops.integrations.mabang.client import REQUEST_INTERVAL, get_mabang_cred
from sellerops.integrations.mabang.orders import (
    CancelRejected, cancel_pending_order, fetch_pending_orders, invoiced_orders,
)
from sellerops.integrations.mabang.session import MabangAuthError
from sellerops.integrations.wildberries.fbs_api import BATCH_SIZE, WBFBSAPI, valid_supply_id
from sellerops.persistence import fbs_shipments, mabang_voids
from sellerops.persistence.price_book import SHIPPING_MODE_WB
from sellerops.settings import paths


_WB_STATUSES = frozenset({
    'waiting', 'sorted', 'sold', 'ready_for_pickup', 'postponed_delivery',
    'sent_to_carrier', 'accepted_by_carrier', 'delivering_by_carrier',
})
_WB_NONDELIVERY = frozenset({
    'canceled', 'canceled_by_client', 'declined_by_client', 'defect', 'canceled_by_carrier',
})
_SUPPLIER_STATUSES = frozenset({'new', 'confirm', 'complete', 'cancel', 'cancel_carrier'})
_READ_ERRORS = (SellerOpsError, requests.RequestException, RuntimeError, ValueError)
_BLOCKING = frozenset({'BLOCKED', 'FAILED', 'UNKNOWN', 'NOT_SUBMITTED'})
_PENDING_EMPTY = ('', '--')
_MABANG_PROGRESS = ('已预报', '待预报', '预报中', '预报失败', '预报成功', '待上传',
                    '已交运', '已发货', 'logisticsChannelText')


@dataclass
class OrderResult:
    shop_id: int
    supply_id: str
    platform_order_id: int
    mabang_order_id: int | None = None
    vc: str = ''
    shipping_mode: str = ''
    supplier_status: str = ''
    wb_status: str = ''
    pending_state: str = ''
    invoice_state: str = ''
    request_attempted: bool = False
    status: str = 'CHECKING'
    reason: str = ''

    @property
    def key(self) -> tuple[int, int]:
        return self.shop_id, self.platform_order_id

    def mark(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason


def _order_id(value: object) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    try:
        return _order_id(int(value))
    except ValueError:
        return None


def _select_supplies(supplies: list[dict], raw_shops: str, supply_id: str) -> list[dict]:
    values = [_order_id(v) for v in split_values(raw_shops)] if raw_shops else []
    shop_ids = set(values)
    if None in shop_ids or len(values) != len(shop_ids):
        raise ValueError('--shops 须为逗号分隔且不重复的正整数店铺 ID')
    scoped = [row for row in supplies if not shop_ids or row['shop_id'] in shop_ids]
    if supply_id:
        if not valid_supply_id(supply_id):
            raise ValueError('--supply-id 供货号格式无效')
        scoped = [row for row in scoped if row.get('supply_id') == supply_id]
        if len(scoped) != 1:
            raise ValueError('供货号在所选范围内未找到或无法唯一确认店铺')
        return scoped
    return [row for row in scoped if row['stage'] in ('delivered', 'complete')]


def _accepted(supply: dict) -> bool:
    if supply['stage'] not in ('delivered', 'complete') or not valid_supply_id(supply.get('supply_id')):
        return False
    attempts = supply['attempts']
    if not attempts or any(not isinstance(a, dict) or a.get('result') != 'accepted' for a in attempts):
        return False
    if attempts[0].get('step') == 'create':
        if attempts[0].get('http_status') != 201:
            return False
        attempts = attempts[1:]
    if len(attempts) < 2 or attempts[-1].get('step') != 'deliver' or attempts[-1].get('http_status') != 204:
        return False
    ids = []
    for attempt in attempts[:-1]:
        added = attempt.get('order_ids')
        if (attempt.get('step') != 'add' or attempt.get('http_status') != 204
                or not isinstance(added, list) or not added
                or any(type(oid) is not int or oid <= 0 for oid in added)):
            return False
        ids.extend(added)
    return len(ids) == len(supply['order_ids']) and set(ids) == set(supply['order_ids'])


def _shop_labels(shop_map: dict) -> dict[int, list[str]]:
    if not isinstance(shop_map, dict):
        raise ValueError('mabang.shop_map 必须是对象')
    labels = defaultdict(list)
    for mabang_name, label in shop_map.items():
        suffix = re.fullmatch(r'.+\(([0-9]+)\)\s*', label) if isinstance(label, str) else None
        wb_shop_id = _order_id(suffix.group(1)) if suffix else None
        if wb_shop_id is not None and isinstance(mabang_name, str) and mabang_name.strip():
            labels[wb_shop_id].append(mabang_name)
    return labels


def _pending_evidence(order: dict) -> str:
    if tuple(_order_id(order.get(k)) for k in ('orderStatus', 'isNewOrder', 'tableBase')) != (2, 1, 1):
        return '马帮未确认处于新增待处理配货库'
    if order.get('platformIdText') != 'Wildberries':
        return '马帮订单的平台未确认为 Wildberries'
    label, channel = order.get('order_label'), order.get('cansend1logisticsHtml')
    if not isinstance(label, str) or not isinstance(channel, str):
        return '马帮预报或交运标记缺失'
    markup = unescape(label + ' ' + channel)
    text = re.sub(r'<[^>]*>', '', markup)
    if any(marker in markup or marker in text for marker in _MABANG_PROGRESS):
        return '马帮有预报、物流渠道或交运标记'
    for field in ('logisticsId', 'trackNumber', 'transportTime', 'deliver_real_time',
                  'orderDeliverTime', 'expressTime', 'timeCancel'):
        if order.get(field) not in _PENDING_EMPTY:
            return '马帮有运单、发货或取消状态，或相关字段缺失'
    return ''


def _check_product(row: OrderResult, order: dict, product_name: str, names: dict, modes: dict) -> None:
    summary = order.get('order_ellipsis_other_title')
    matched_summary = order.get('order_ellipsis_title')
    if (not isinstance(summary, str) or not isinstance(matched_summary, str)
            or summary.count('数量:') != 1 or matched_summary.count('数量:') != 1
            or re.findall(r'商品种类:(\d+)', summary) != ['1']
            or re.findall(r'商品种类:(\d+)', matched_summary) != ['1']
            or order.get('order_combosku_data') not in ('[]', [])):
        row.mark('BLOCKED', '马帮无法确认订单只有一种商品及完整 VC')
        return
    vc = VC_RE.fullmatch(summary.partition('数量:')[2].strip())
    row.vc = vc.group(1) if vc else ''
    row.shipping_mode = modes.get(row.vc) or ''
    if not row.vc or row.shipping_mode != SHIPPING_MODE_WB or names.get(row.vc) != product_name:
        row.mark('BLOCKED', '当前完整 VC、发货方式或商品中文名与 WB 供货不一致')


def _candidates(results: list[OrderResult], supplies: list[dict], pending: list[dict], shop_map: dict,
                attempts: dict, *, explicit: bool) -> list[tuple[OrderResult, dict, str]]:
    labels = _shop_labels(shop_map)
    by_key = defaultdict(list)
    platform_ids, internal_ids = set(), Counter()
    for order in pending:
        platform_id = _order_id(order.get('platformOrderId'))
        name = order.get('shopIdText')
        internal_ids[_order_id(order.get('id'))] += 1
        if platform_id is not None:
            platform_ids.add(platform_id)
            if isinstance(name, str):
                by_key[name, platform_id].append(order)
    attempted_ids = {a['mabang_order_id'] for a in attempts.values()}
    candidates = []
    supply_counts = Counter((s['shop_id'], s['supply_id']) for s in supplies)
    for supply in supplies:
        valid = _accepted(supply) and supply_counts[supply['shop_id'], supply['supply_id']] == 1
        shop_id, supply_id = supply['shop_id'], supply['supply_id']
        mapped_names = labels.get(shop_id, [])
        for platform_id in supply['order_ids']:
            previous = attempts.get((shop_id, platform_id))
            matches = by_key.get((mapped_names[0], platform_id), []) if len(mapped_names) == 1 else []
            if not explicit and not matches and not (
                    (previous and previous['status'] in ('SUBMITTING', 'UNKNOWN'))
                    or (len(mapped_names) != 1 and platform_id in platform_ids)):
                continue
            row = OrderResult(shop_id, supply_id, platform_id)
            results.append(row)
            if previous:
                row.mabang_order_id = previous['mabang_order_id']
                if previous['status'] == 'ACCEPTED':
                    row.mark('SKIPPED', '本地已记录马帮受理')
                elif previous['status'] == 'FAILED':
                    row.mark('FAILED', '既有提交被拒绝，待人工处理')
                else:
                    row.mark('UNKNOWN', '既有提交中或结果未知，待人工核对')
                continue
            if not valid:
                row.mark('BLOCKED', '供货日志缺少完整的 WB 加单和配送受理证据')
            elif len(mapped_names) != 1:
                row.mark('BLOCKED', 'mabang.shop_map 无法唯一确认目标店铺')
            elif not matches:
                row.mark('SKIPPED', '马帮目标店待处理未找到此平台单号')
            elif len(matches) != 1:
                row.mark('BLOCKED', '马帮目标店有重复平台单号')
            else:
                order = matches[0]
                row.mabang_order_id = _order_id(order.get('id'))
                if (row.mabang_order_id is None or internal_ids[row.mabang_order_id] != 1
                        or row.mabang_order_id in attempted_ids):
                    row.mark('BLOCKED', '马帮内部订单 ID 缺失、重复或已用于其他提交')
                    continue
                row.pending_state = _pending_evidence(order)
                if row.pending_state:
                    row.mark('BLOCKED', row.pending_state)
                else:
                    row.pending_state = '已确认待处理，未预报或交运'
                    candidates.append((row, order, supply['product_name']))
    return candidates


def _http_status(exc: Exception) -> int | None:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code
    return getattr(exc, 'status_code', None)


def _read_wb(rows: list[OrderResult]) -> None:
    by_shop = defaultdict(list)
    for row in rows:
        if row.status == 'CHECKING':
            by_shop[row.shop_id].append(row)
    if not by_shop:
        return
    shops = {shop.id: shop for shop in bcs.fetch_open_api_shops()}
    for shop_id, shop_rows in by_shop.items():
        shop = shops.get(shop_id)
        if shop is None or not shop.api_key or paths.is_shop_archived(shop_id):
            for row in shop_rows:
                row.mark('BLOCKED', '目标店不可用、已归档或缺 WB Open API 令牌')
            continue
        api = None
        try:
            api = WBFBSAPI(shop.api_key)
            _check_wb_shop(api, shop_rows)
        except _READ_ERRORS as exc:
            code = _http_status(exc)
            for row in shop_rows:
                if row.status == 'CHECKING':
                    row.mark('BLOCKED', f'WB HTTP {code}，已停止该店' if code in (401, 403, 429)
                             else 'WB 店铺读取失败')
        finally:
            if api is not None:
                api.close()


def _check_wb_shop(api: WBFBSAPI, rows: list[OrderResult]) -> None:
    by_supply = defaultdict(list)
    for row in rows:
        by_supply[row.supply_id].append(row)
    for supply_id, grouped in by_supply.items():
        try:
            members = api.supply_order_ids(supply_id)
        except _READ_ERRORS as exc:
            if _http_status(exc) in (401, 403, 429):
                raise
            for row in grouped:
                row.mark('BLOCKED', '无法核实 WB 供货成员')
            continue
        for row in grouped:
            if row.platform_order_id not in members:
                row.mark('BLOCKED', '订单不在 WB 当前供货成员中')
    candidates = [row for row in rows if row.status == 'CHECKING']
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        statuses = api.order_statuses([r.platform_order_id for r in batch])
        for row in batch:
            status = statuses.get(row.platform_order_id, {})
            supplier, wb = status.get('supplierStatus'), status.get('wbStatus')
            if isinstance(supplier, str) and supplier in _SUPPLIER_STATUSES:
                row.supplier_status = supplier
            if isinstance(wb, str) and (wb in _WB_STATUSES or wb in _WB_NONDELIVERY):
                row.wb_status = wb
            if row.supplier_status != 'complete' or row.wb_status not in _WB_STATUSES:
                row.mark('BLOCKED', 'WB 订单未完成转配送，或平台状态不在允许范围')


def _submit(row: OrderResult, cred: dict, persist, *, run_id: str) -> bool:
    attempt = {
        'shop_id': row.shop_id, 'supply_id': row.supply_id, 'platform_order_id': row.platform_order_id,
        'mabang_order_id': row.mabang_order_id, 'run_id': run_id,
        'updated_at': datetime.now(fbs_shipments.SINGAPORE).isoformat(),
        'status': 'SUBMITTING', 'reason': '准备提交',
    }
    try:
        persist(attempt)
    except OSError:
        row.mark('BLOCKED', '马帮作废账本无法落盘，未提交')
        return False
    row.request_attempted = True
    keep_going = True
    try:
        cancel_pending_order(cred, row.mabang_order_id)
    except CancelRejected:
        row.mark('FAILED', '马帮明确拒绝作废')
    except KeyboardInterrupt:
        row.mark('UNKNOWN', '作废提交中断，待人工核对')
        raise
    except _READ_ERRORS as exc:
        row.mark('UNKNOWN', '马帮作废回执不确定，待人工核对')
        keep_going = not (isinstance(exc, MabangAuthError) or _http_status(exc) in (401, 403, 429))
    else:
        row.mark('ACCEPTED', '马帮已受理')
    attempt.update(status=row.status, reason=row.reason, updated_at=datetime.now(fbs_shipments.SINGAPORE).isoformat())
    try:
        persist(attempt)
    except OSError:
        row.mark('UNKNOWN', '作废提交后无法写入账本，待人工核对')
        return False
    return keep_going


def _check_invoices(rows: list[OrderResult], cred: dict, *, apply: bool, persist, run_id: str) -> None:
    keep_going = True
    candidates = [row for row in rows if row.status == 'CHECKING']
    for index, row in enumerate(candidates):
        if not keep_going:
            row.mark('NOT_SUBMITTED', '本轮马帮会话、限流或账本异常后未提交')
            continue
        if index:
            time.sleep(REQUEST_INTERVAL)
        try:
            invoiced = invoiced_orders(cred, [row.mabang_order_id])
        except _READ_ERRORS as exc:
            row.invoice_state = '未知'
            row.mark('BLOCKED', '马帮发票校验失败')
            keep_going = not (isinstance(exc, MabangAuthError) or _http_status(exc) in (401, 403, 429))
            continue
        if invoiced:
            row.invoice_state = '已开票'
            row.mark('BLOCKED', '已开票订单需人工处理')
            continue
        row.invoice_state = '未开票'
        if apply:
            keep_going = _submit(row, cred, persist, run_id=run_id)
        else:
            row.mark('READY', '可预览作废')


def _run_locked(args, results: list[OrderResult], run_id: str) -> None:
    supplies = _select_supplies(fbs_shipments.load_journal(fbs_shipments.journal_path()),
                                args.shops, args.supply_id)
    if not supplies:
        return
    if args.supply_id and not _accepted(supplies[0]):
        supply = supplies[0]
        for oid in supply['order_ids']:
            row = OrderResult(supply['shop_id'], supply['supply_id'], oid)
            row.mark('BLOCKED', '供货日志缺少完整的 WB 加单和配送受理证据')
            results.append(row)
        return
    path = mabang_voids.ledger_path()
    try:
        attempts = mabang_voids.load_attempts(path)
        cred = get_mabang_cred()
        pending = fetch_pending_orders(cred)
        candidates = _candidates(results, supplies, pending, cred['shop_map'], attempts,
                                 explicit=bool(args.supply_id))
    except _READ_ERRORS + (OSError,):
        if args.supply_id and not results:
            supply = supplies[0]
            results.extend(OrderResult(supply['shop_id'], supply['supply_id'], oid)
                           for oid in supply['order_ids'])
        raise
    if not candidates:
        return
    names, _, modes = load_local_mapping()
    for row, order, product_name in candidates:
        _check_product(row, order, product_name, names, modes)
    _read_wb(results)

    def persist(attempt: dict) -> None:
        attempts[attempt['shop_id'], attempt['platform_order_id']] = dict(attempt)
        mabang_voids.save_attempts(path, attempts)

    _check_invoices(results, cred, apply=args.apply, persist=persist, run_id=run_id)


def _write_rows(stream, rows: list[dict], *, run_id: str, mode: str, timestamp: str) -> None:
    columns = ['run_id', 'mode', 'timestamp', *OrderResult.__dataclass_fields__]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row, run_id=run_id, mode=mode, timestamp=timestamp))


def run(args) -> int:
    ensure_utf8_stdout()
    now = datetime.now(fbs_shipments.SINGAPORE)
    run_id = f'{now:%Y%m%d_%H%M%S}_{uuid4().hex[:8]}'
    mode = 'apply' if args.apply else 'preview'
    results = []
    error, interrupted = '', False
    try:
        with mabang_voids.fulfillment_lock():
            _run_locked(args, results, run_id)
    except KeyboardInterrupt:
        error, interrupted = '操作已中断，请核对提交记录', True
    except _READ_ERRORS + (OSError,) as exc:
        error = str(exc) if type(exc) is ValueError else f'取证或本地状态读取失败（{type(exc).__name__}）'
    for row in results:
        if row.status == 'CHECKING':
            row.mark('NOT_SUBMITTED' if interrupted else 'BLOCKED', error or '未完成校验')
        print(f'  [{row.status}] 店{row.shop_id}｜{row.supply_id}｜{row.platform_order_id}'
              f'｜马帮 {row.mabang_order_id or "-"}｜{row.reason}')
    rows = [asdict(row) for row in results]
    counts = dict(Counter(r.status for r in results))
    args.output = {'run_id': run_id, 'mode': mode, 'counts': counts, 'rows': rows}
    csv_path = ''
    try:
        csv_path = write_csv_preferred(
            str(paths.daily_log_dir(now)), '马帮WB供货作废_{ts}.csv', 'mabang_void_wb_{ts}.csv',
            run_id, partial(_write_rows, rows=rows, run_id=run_id, mode=mode, timestamp=now.isoformat()),
        )
    except OSError:
        pass
    args.output['artifacts'] = {'csv': csv_path}
    if error:
        args.output['error'] = error
        print(f'[中止] {error}')
    args.summary = f'马帮 WB 供货作废{"提交" if args.apply else "预览"}｜{len(rows)} 单'
    if not csv_path:
        args.summary += '｜CSV 写入失败'
    if counts.keys() & {'UNKNOWN', 'FAILED'}:
        args.next_action = '查看回执和马帮订单状态，勿重发已经提交或结果未知的订单'
    print(f'{args.summary}｜明细 {csv_path or "未生成"}')
    return 130 if interrupted else int(bool(error or not csv_path or counts.keys() & _BLOCKING))

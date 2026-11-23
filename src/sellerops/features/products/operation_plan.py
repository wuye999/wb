"""固定计划单及唯一提交时的状态转移。"""
import json
import re
from collections.abc import Callable
from argparse import Namespace

from sellerops.features.catalog.shops import require_active
from sellerops.features.products.plans import validate_price_intent
from sellerops.persistence import db


PRODUCT_ACTIONS = {'price', 'stock', 'discount', 'trash', 'review-price'}
PUBLISH_ACTIONS = {'publish', 'create-cards', 'replicate', 'publish-openapi', 'normalize-vc'}
ACTIONS = PRODUCT_ACTIONS | PUBLISH_ACTIONS


def has_work(prepared: list | dict | None) -> bool:
    if isinstance(prepared, list):
        return any(plan['items'] for plan in prepared)
    return bool(prepared and (prepared.get('items') or prepared.get('create') or prepared.get('continue')))


def preview(args: Namespace, prepare: Callable[[Namespace], int]) -> int:
    if getattr(args, 'apply', False):
        raise ValueError('请先预览生成计划单，确认后使用 apply --plan 单号提交')
    args.capture_plan = True
    answer = prepare(args)
    frozen = getattr(args, 'prepared', None)
    args.output = {'action': args.action, 'scope': frozen or [], 'result': getattr(args, 'outcome', {})}
    if not has_work(frozen):
        args.summary = getattr(args, 'summary', '') or '没有可提交的商品'
        return answer
    action = args.action
    action_type = ('price' if action == 'review-price' else action.replace('-', '_')
                   if action in PRODUCT_ACTIONS or action == 'normalize-vc' else 'publish')
    private = {'handler', 'json', 'cmd', 'output', 'summary', 'next_action', 'plan_id',
               'apply', 'prepared', 'capture_plan', 'outcome'}
    payload = {'route': action, 'arguments': {k: v for k, v in vars(args).items() if k not in private},
               'scope': frozen}
    with db.connect(write=True) as connection:
        number = connection.execute("""
            SELECT coalesce(max(CAST(substr(plan_id, 2) AS INTEGER)), 0) + 1
              FROM plans WHERE plan_id GLOB 'P[0-9]*'
        """).fetchone()[0]
        plan_id = f'P{number:03d}'
        db.save_plan(connection, plan_id, action_type, 'draft', plan_id, payload)
    args.plan_id = plan_id
    args.summary = f'已保存计划单 {plan_id}'
    if answer:
        args.summary += '，部分商品未通过检查'
    args.next_action = f'展示商品、店铺和目标值，运营确认后运行 apply --plan {plan_id}'
    print(f'计划单 {plan_id}。{args.next_action}')
    return answer


def _scope_shops(scope: list | dict) -> set[int]:
    if isinstance(scope, list):
        return {plan['shopId'] for plan in scope if plan['items']}
    return {int(sid) for sid in scope.get('warehouses', {})} | {
        row['shopId'] for key in ('items', 'create', 'continue')
        for row in scope.get(key, []) if isinstance(row, dict) and 'shopId' in row
    }


def _verify_warehouses(scope: list | dict) -> None:
    from sellerops.features.products.warehouse import resolve_warehouse
    if isinstance(scope, list):
        pairs = {(plan['shopId'], row['warehouseId']) for plan in scope if plan['items']
                 for row in plan.get('warehouses', [])}
        for plan in scope:
            for item in plan['items']:
                pairs.update((plan['shopId'], wid) for _, wid in item.get('stock_specs', []))
    else:
        pairs = {(int(sid), wh) for sid, wh in scope.get('warehouses', {}).items()}
    for sid, expected in sorted(pairs):
        if resolve_warehouse(sid) != expected:
            raise ValueError(f'店铺 {sid} 的莫斯科仓 ID 已变化或无法核对，请重新预览')


def _unknown(outcome: dict) -> bool:
    return any(row.get('status') in {'unknown', 'submitting'}
               for key in ('receipts', 'items') for row in outcome.get(key, []))


def apply(args: Namespace, execute: Callable[[str, Namespace], int]) -> int:
    plan_id = args.plan.strip().upper()
    if not re.fullmatch(r'P[0-9]+', plan_id):
        raise ValueError('计划单号格式应为 P 加数字')
    args.plan_id = plan_id
    with db.connect(write=True) as connection:
        record = connection.execute('SELECT * FROM plans WHERE plan_id=?', (plan_id,)).fetchone()
        if record is None:
            raise ValueError(f'找不到计划单 {plan_id}')
        if record['status'] != 'draft':
            args.output = {'status': record['status']}
            raise ValueError(f'计划单 {plan_id} 状态为 {record["status"]}，禁止重复提交')
        document = json.loads(record['payload_json'])
        if document['route'] not in ACTIONS or not has_work(document['scope']):
            raise ValueError('计划单内容无效')
        options = Namespace(**document['arguments'])
        if document['route'] == 'price':
            validate_price_intent(options.price, options.keep_price, options.discount, options.club_discount)
        if document['route'] in {'publish', 'create-cards'}:
            items = (row['item'] if document['route'] == 'publish' else row[0] for row in document['scope']['items'])
            if any(not item['extra'].get('seller_sku') or not item['extra'].get('vc_source') for item in items):
                raise ValueError('原上架计划缺少卖家 SKU 或编码来源，请用 ops 重新预览')
        connection.execute("UPDATE plans SET status='submitting', updated_at=? WHERE plan_id=? AND status='draft'",
                           (db.now(), plan_id))
    scope = document['scope']
    options.apply, options.yes, options.frozen = True, True, scope
    options.json = getattr(args, 'json', False)
    outcome, status, code = {}, 'partial', 1
    try:
        for sid in sorted(_scope_shops(scope)):
            require_active(sid)
        _verify_warehouses(scope)
    except (ValueError, OSError) as error:
        outcome = {'error': str(error)}
    else:
        try:
            code = execute(document['route'], options)
            outcome = getattr(options, 'outcome', {})
            status = 'unknown' if _unknown(outcome) or (code and not outcome) else 'partial' if code else 'done'
        except Exception as error:
            status = 'unknown'
            outcome = {'error': type(error).__name__}
    document['receipt'] = outcome
    with db.connect(write=True) as connection:
        db.save_plan(connection, plan_id, record['action_type'], status, record['dedup_key'], document)
    args.output = {'status': status, 'receipt': outcome}
    args.summary = f'计划单 {plan_id} 已记录提交结果' if code == 0 else f'计划单 {plan_id} 未全部受理'
    if code:
        args.next_action = '先查看回执；提交中或结果未知的商品禁止自动重发'
    if outcome.get('mapping_conflicts'):
        args.summary = f'计划单 {plan_id} 已提交，部分已受理 VC 的归属有冲突'
        args.next_action = '核对回执中的冲突 VC，并用 map 确认归属；不要重新上架'
    print(args.summary)
    return code

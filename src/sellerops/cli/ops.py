"""生成固定计划单，并通过单号调用原有提交入口。"""
import argparse

from sellerops.cli import listing, product
from sellerops.cli.binding import bind
from sellerops.features.products import operation_plan


def register(sub: argparse._SubParsersAction) -> None:
    product.register(sub, planned_only=True)
    listing.register(sub, planned_only=True)
    for parser in sub.choices.values():
        parser.add_argument('--json', action='store_true', help='输出单个 JSON 对象')


def register_apply(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser('apply', help='按运营确认的固定计划单提交')
    parser.add_argument('--plan', required=True, help='预览生成的计划单号')
    parser.add_argument('--json', action='store_true', help='输出单个 JSON 对象')
    bind(parser, 'sellerops.cli.ops', 'run_apply')


def run_apply(args) -> int:
    return operation_plan.apply(args, execute_frozen)


def execute_frozen(action: str, args) -> int:
    if action == 'review-price':
        from sellerops.features.products.price_review import run
        return run(args)
    if action in operation_plan.PRODUCT_ACTIONS:
        return getattr(product, 'run_' + action)(args)
    if action == 'normalize-vc':
        from sellerops.features.publishing.vc_normalization import apply_frozen
        return apply_frozen(args)
    if action == 'publish-openapi':
        from sellerops.features.publishing.open_api_publish import apply_frozen
        return apply_frozen(args)
    from sellerops.features.publishing import bulk_publish, card_publish

    executor = bulk_publish if action in {'publish', 'import'} else card_publish
    result = executor.execute_publish([], apply=True, interval=args.interval, frozen=args.frozen)
    args.outcome = {k: result[k] for k in ('ok', 'skip', 'fail', 'csv')}
    args.outcome['items'] = [
        {'vc': row['vc'], 'targets': row['targets'],
         'task_id': row.get('task_id') or ((row.get('response') or {}).get('data') or {}).get('taskId'),
         'status': row.get('status') or ('accepted' if row['ok'] else 'unknown' if row.get('response') is None else 'failed')}
        for row in result.get('details', [])
    ]
    if result['fail'] and not result.get('details'):
        args.outcome['items'].append({'status': 'unknown'})
    return int(bool(result['fail']))

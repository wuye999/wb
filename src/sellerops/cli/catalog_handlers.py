from sellerops.features.catalog import mapping_sync as catalog_mapping_sync
from sellerops.features.catalog import mismatch_check as catalog_mismatch_check
from sellerops.features.catalog import products as catalog_products
from sellerops.persistence.price_book import PRICE_BOOK_EXAMPLE
from sellerops.persistence.snapshots import CACHE_STATUSES


_PREFIX_SKIP_LABELS = {
    'blocked': '禁上架', 'invalid_vc_prefix': '完整 VC 无有效前缀',
    'ambiguous_prefix': '价格表前缀冲突', 'unmatched_prefix': '未命中价格表前缀',
}
_PREFIX_SKIP_ACTIONS = {
    'missing_price_book': (f'将 {PRICE_BOOK_EXAMPLE} 复制到本次数据目录并改名为 商品价格表.xlsx，'
                           '填写唯一四位英文字母前缀后按需同步受影响店铺'),
    'no_usable_prefix': '在价格表中填写四位英文字母前缀并处理重复项，再按需同步受影响店铺',
    'invalid_price_book': '运行 doctor 核对并修正价格表，再按需同步受影响店铺',
    'unreadable_price_book': '核对价格表读取权限，如被 Excel 占用则保存并关闭，再按需同步受影响店铺',
    'price_book_changed': '确认价格表已保存，再按需同步受影响店铺',
}


def run_shops(args):
    from sellerops.integrations.bcs.client import client
    shops = client.fetch_shop_list()
    if args.name:
        matched = [shop for shop in shops if shop["name"] == args.name]
        if len(matched) != 1:
            print("店名未唯一匹配，请核对以下店铺 ID")
            for shop in matched or [s for s in shops if args.name in s["name"]]:
                print(f"{shop['id']} | {shop['name']}")
            return 1
        shops = matched
    for shop in shops:
        print(f"{shop['id']} | {shop['name']}")
    return 0


def _print_mapping_summary(receipt: dict) -> None:
    mapping = receipt['prefix_mapping']
    pending = receipt['mapping_counts']['pending_vcs']
    remaining = '未知，请核对本地库' if pending is None else f'{pending} 个 VC'
    print(f"[归属汇总] 自动新增归属 {mapping['mapped']} 个 VC，成功保存店铺剩余待认领 {remaining}")
    reasons = mapping['skipped_vcs']
    if reasons:
        details = '、'.join(f'{label} {reasons[code]} 次'
                           for code, label in _PREFIX_SKIP_LABELS.items() if reasons.get(code))
        print(f'  前缀归属跳过（按店计次）{details}')


def _report_fetch_followup(receipt: dict) -> list[str]:
    actions = []
    if receipt['missing_shops']:
        actions.append('请运营确认缺失店铺后再归档')
    if receipt['errors']:
        actions.append('根据失败店铺的原因核对权限、连接或店铺 ID，确认后只同步受影响的店铺')

    fetched = receipt['fetched']
    skipped = receipt['prefix_mapping']['skipped_shops']
    if skipped:
        details = '、'.join(f"店铺 {row['id']}（{row['reason']}）" for row in skipped)
        print(f'[前缀归属提醒] 以下店铺跳过自动前缀归属。{details}')
        for code in dict.fromkeys(row['prefix_skip_code'] for row in fetched if row.get('prefix_skip_code')):
            actions.append(_PREFIX_SKIP_ACTIONS[code])

    cached = [str(row['id']) for row in fetched if row['snapshot_status'] in CACHE_STATUSES]
    if cached:
        target = f'--shop-id {cached[0]}' if len(cached) == 1 else f"--shops {','.join(cached)}"
        actions.append(f'需要确认缓存店铺的快照时，可在同一数据目录运行 sync {target}')
    if receipt['prefix_mapping']['mapped']:
        actions.append('用 map check-mismatch 看图复核新增归属')
    pending = receipt['mapping_counts']['pending_vcs']
    if pending:
        actions.append('用 map workbench 看图处理剩余待认领')
    elif pending is None:
        actions.append('核对本地 sellerops.db 后再查看归属统计')
    return actions


def run_fetch(args):
    parallel = getattr(args, 'parallel', 4)
    if parallel < 1:
        raise ValueError('并发店铺数必须为正整数')
    shop_id = getattr(args, 'shop_id', None)
    no_sync = getattr(args, 'no_sync', False)
    source = getattr(args, 'source', 'bcs')
    needs_refresh = getattr(args, 'needs_refresh', False)
    scope = getattr(args, 'shops', None)
    if shop_id is not None and not needs_refresh:
        args.output = catalog_products.fetch_single_status(shop_id, no_sync=no_sync, source=source)
    else:
        selected = [int(item) for item in scope.split(',')] if scope else None
        if shop_id is not None:
            selected = [shop_id]
        args.output = catalog_products.fetch_all(no_sync=no_sync, source=source, parallel=parallel,
                                                bcs_parallel=getattr(args, 'bcs_parallel', 2),
                                                shop_ids=selected, needs_refresh=needs_refresh)
    errors = args.output['errors']
    fetched = args.output['fetched']
    fallback = sum(row['snapshot_status'] == 'bcs_cache_fallback' for row in fetched)
    cache_only = sum(row['snapshot_status'] == 'bcs_cache_only' for row in fetched)
    cache_summary = (f'{fallback} 家店铺使用 BCS 缓存（同步未确认）' if fallback else
                     f'{cache_only} 家店铺读取 BCS 缓存（已跳过同步）' if cache_only else '')
    if errors:
        args.summary = ('同步失败' if shop_id is not None else '同步部分失败' if fetched else '同步全部失败')
        if cache_summary:
            args.summary += f'，已保存快照中有 {cache_summary}'
        elif source == 'bcs' and no_sync:
            args.summary = '读取 BCS 缓存失败（已跳过同步）'
    elif cache_summary:
        args.summary = f'快照已保存，{cache_summary}'
    elif not fetched:
        args.summary = ('所选范围内没有待补刷店铺'
                        if needs_refresh and not args.output['missing_shops'] else '无可同步的店铺')
    else:
        args.summary = '快照同步完成'
    print(f"[快照汇总] {args.summary}。已保存 {len(fetched)} 家，失败 {len(errors)} 家")
    if fetched:
        _print_mapping_summary(args.output)
    args.next_action = '。'.join(_report_fetch_followup(args.output))
    if args.next_action:
        print(args.next_action)
    return int(bool(errors))


def run_mismatch_check(args):
    args.output = catalog_mismatch_check.run(cn=args.cn, begin=args.begin, end=args.end, days=args.days)
    args.summary = '已生成货不对板筛查工作台'
    if args.output['counts']['total']:
        args.next_action = '打开 HTML 看图核对，勾选后导出或复制完整 VC'
    return 0

def run_mapping_rename(args):
    return 0 if catalog_mapping_sync.set_vc_override(vc=getattr(args, "vc", ""), new_cn=getattr(args, "cn", ""), reason=getattr(args, "reason", "人工纠偏"), file_path=getattr(args, "file", "")) else 1

def run_shops_mapping(args):
    catalog_mapping_sync.sync_all_shops_mapping(shop_id=getattr(args, "shop_id", None))
    return 0

from sellerops.features.catalog import mapping_sync as catalog_mapping_sync
from sellerops.features.catalog import mismatch_check as catalog_mismatch_check
from sellerops.features.catalog import products as catalog_products

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


def run_fetch(args):
    from sellerops.persistence import db

    with db.connect(read_only=True):
        pass
    parallel = getattr(args, 'parallel', 4)
    if parallel < 1:
        raise ValueError('并发店铺数必须为正整数')
    shop_id = getattr(args, 'shop_id', None)
    no_sync = getattr(args, 'no_sync', False)
    source = getattr(args, 'source', 'bcs')
    if shop_id is not None:
        args.output = catalog_products.fetch_single_status(shop_id, no_sync=no_sync, source=source)
    else:
        args.output = catalog_products.fetch_all(no_sync=no_sync, source=source, parallel=parallel,
                                                  bcs_parallel=getattr(args, 'bcs_parallel', 2))
    if args.output is None:
        return 0
    if args.output['missing_shops']:
        args.next_action = '\u8bf7\u8fd0\u8425\u786e\u8ba4\u7f3a\u5931\u5e97\u94fa\u540e\u518d\u5f52\u6863'
    elif args.output.get('errors'):
        args.next_action = '根据失败店铺的原因核对权限或连接，确认后只同步受影响的店铺'
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
        args.summary = '无可同步的店铺'
    else:
        args.summary = '快照同步完成'
    print(f"[快照汇总] {args.summary}。已保存 {len(fetched)} 家，失败 {len(errors)} 家")
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

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
    from sellerops.features.catalog.shops import require_active

    with db.connect():
        pass
    parallel = getattr(args, 'parallel', 4)
    if parallel < 1:
        raise ValueError('并发店铺数必须为正整数')
    if getattr(args, 'shop_id', None) is not None:
        require_active(args.shop_id)
        _, total = catalog_products.fetch_shop(args.shop_id, no_sync=getattr(args, 'no_sync', False),
                                                source=getattr(args, 'source', 'bcs'))
        args.output = {'fetched': [{'id': args.shop_id, 'total': total}], 'missing_shops': []}
    else:
        args.output = catalog_products.fetch_all(no_sync=getattr(args, 'no_sync', False),
                                                  source=getattr(args, 'source', 'bcs'), parallel=parallel)
    if args.output is None:
        return 0
    if args.output['missing_shops']:
        args.next_action = '\u8bf7\u8fd0\u8425\u786e\u8ba4\u7f3a\u5931\u5e97\u94fa\u540e\u518d\u5f52\u6863'
    elif args.output.get('errors'):
        args.next_action = '根据失败店铺的原因核对权限或连接，确认后只同步受影响的店铺'
    if args.output.get('errors'):
        args.summary = '同步部分失败'
    elif warnings := args.output.get('sync_warnings'):
        args.summary = f'快照已保存，{len(warnings)} 家店铺使用 BCS 缓存'
    else:
        args.summary = '快照同步完成'
    return int(bool(args.output.get('errors')))


def run_mismatch_check(args):
    args.output = catalog_mismatch_check.run(cn=args.cn, begin=args.begin, end=args.end, days=args.days)
    args.summary = '已生成货不对板筛查工作台'
    args.next_action = '打开 HTML 看图核对，勾选后导出或复制完整 VC'
    return 0

def run_mapping_rename(args):
    return 0 if catalog_mapping_sync.set_vc_override(vc=getattr(args, "vc", ""), new_cn=getattr(args, "cn", ""), reason=getattr(args, "reason", "人工纠偏"), file_path=getattr(args, "file", "")) else 1

def run_shops_mapping(args):
    catalog_mapping_sync.sync_all_shops_mapping(shop_id=getattr(args, "shop_id", None))
    return 0

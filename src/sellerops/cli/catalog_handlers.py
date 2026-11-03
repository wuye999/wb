from sellerops.features.catalog import mapping as catalog_mapping
from sellerops.features.catalog import mapping_check as catalog_mapping_check
from sellerops.features.catalog import mapping_sync as catalog_mapping_sync
from sellerops.features.catalog import mismatch_check as catalog_mismatch_check
from sellerops.features.catalog import products as catalog_products

def run_fetch(args):
    from sellerops.settings import paths as config
    parallel = getattr(args, 'parallel', 4)
    if parallel < 1:
        raise ValueError('并发店铺数必须为正整数')
    if getattr(args, "shop_id", None) is not None:
        out = config.shop_json_path(args.shop_id)
        catalog_products.fetch_shop(args.shop_id, out, no_sync=getattr(args, "no_sync", False),
                                    source=getattr(args, 'source', 'wb-openapi'))
    else:
        catalog_products.fetch_all(no_sync=getattr(args, "no_sync", False),
                                   source=getattr(args, 'source', 'wb-openapi'), parallel=parallel)
    return 0

def run_mapping(args):
    catalog_mapping.run_mapping(main_shop_only=getattr(args, "main_shop_only", False))
    return 0

def run_mapping_import(args):
    catalog_mapping.import_mapping(args.file)
    return 0

def run_mapping_check(args):
    catalog_mapping_check.run(tol=getattr(args, "tol", 5))
    return 0

def run_mismatch_check(args):
    catalog_mismatch_check.run(cn=getattr(args, "cn", ""), begin=getattr(args, "begin", ""), end=getattr(args, "end", ""), days=getattr(args, "days", 0))
    return 0

def run_review(args=None):
    catalog_mapping_sync.run_review()
    return 0

def run_merge(args=None):
    file_path = getattr(args, "file", None) if args else None
    catalog_mapping_sync.run_merge(file_path)
    return 0

def run_mapping_rename(args):
    return 0 if catalog_mapping_sync.set_vc_override(vc=getattr(args, "vc", ""), new_cn=getattr(args, "cn", ""), reason=getattr(args, "reason", "人工纠偏"), file_path=getattr(args, "file", "")) else 1

def run_shops_mapping(args):
    catalog_mapping_sync.sync_all_shops_mapping(shop_id=getattr(args, "shop_id", None), force=getattr(args, "force", False))
    return 0

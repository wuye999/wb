"""商品归属纠偏与映射报表导出。"""
from pathlib import Path

from sellerops.settings import paths as config
from sellerops.core.files import safe_load_json
from sellerops.persistence.mappings import MappingRepository
from sellerops.features.catalog import mapping, products, single_shop
from sellerops.features.catalog.vc_mapping import update_mappings


def set_vc_override(vc: str = '', new_cn: str = '', reason: str = '人工纠偏',
                    file_path: str = '') -> bool:
    rows = safe_load_json(file_path) if file_path else [{'vc': vc, 'cn': new_cn}]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('纠偏文件应包含商品列表')
    return bool(update_mappings([
        {**row, 'action': 'rebind', 'source': 'manual', 'note': row.get('reason') or reason}
        for row in rows
    ]))


def _export_destination(name: str, output: str = '') -> Path:
    destination = Path(output).resolve() if output else config.current.exports_dir / name
    if (destination.suffix.lower() != '.xlsx' or destination.name == '商品价格表.xlsx'
            or Path(config.current.data_dir) in destination.parents
            or (config.PROJECT_ROOT / 'data') in destination.parents):
        raise ValueError('请选择数据目录外的 .xlsx 报表路径')
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def export_mapping(shop_id: int | None = None, output: str = '') -> Path:
    if shop_id is not None:
        path = _export_destination(f'shop_{shop_id}.xlsx', output)
        single_shop.build_single_shop_xlsx(shop_id, path)
    else:
        from sellerops.features.catalog.mapping_excel import build_xlsx

        state, excluded = MappingRepository.load_mapping_state(require_price=True)
        shops_data, shops = products.load_all_shops()
        main_id = mapping.main_shop(config.current)
        coverage = {}
        for sid, rows in shops_data.items():
            for raw in rows:
                vc = raw.get('vendorCode')
                if vc:
                    coverage.setdefault(vc, {})[sid] = {
                        'price': mapping.price_of(raw), 'stock': mapping.stock_summary(raw),
                        'nmId': raw.get('nmId'), 'createAt': raw.get('createAt'), 'updateAt': raw.get('updateAt'),
                        'title': raw.get('title'), 'repImg': raw.get('repImg'),
                        **{key: raw.get(key) for key in ('discount', 'clubDiscount', 'dimensionsLength',
                           'dimensionsWidth', 'dimensionsHeight', 'dimensionsWeightBrutto')},
                    }
        records = [{'sku': row['sku'], 'cn': row['cn'], 'minimum_price': row['minimum_price'], 'vendorCodes': [vc]}
                   for vc, row in state.items() if vc in coverage]
        path = _export_destination('价格映射表.xlsx', output)
        build_xlsx([], records, mapping.priced_rows(shops_data.get(main_id, [])),
                   excluded_vcs=excluded, shop_coverage=coverage, shops_meta=shops, main_sid=main_id,
                   out_path=path)
    print(f'已导出：{path}')
    return path


def sync_all_shops_mapping(shop_id: int | None = None) -> None:
    if shop_id is not None:
        export_mapping(shop_id)
        return
    for shop in products.snapshot_shops():
        export_mapping(shop['id'])


def run_export(args) -> int:
    path = export_mapping(args.shop_id, args.out)
    args.summary = '已导出商品归属报表'
    args.output = {'artifacts': {'workbook': str(path.resolve())}}
    return 0

"""按目标店、已确认归属和显式条件选择商品。"""
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.settings import credentials


def load_shop_rows(sid):
    try:
        rows = ProductSnapshotRepository.load_shop_rows(sid)
    except Exception as error:
        print(f'  [快照不可读] 店{sid}：{type(error).__name__}，该店目标将记录到跳过报告')
        return None
    if rows is None:
        print(f'  [跳过] 店{sid} 无可用快照，可单独运行 sellerops catalog fetch')
    return rows


def selected_shops(args):
    if args.shops:
        return list(dict.fromkeys(int(value.strip()) for value in args.shops.split(',')))
    configured = credentials.get().wb.get('shops', [])
    with db.connect() as connection:
        archived = {int(row[0]) for row in connection.execute(
            "SELECT shop_id FROM shops WHERE status = 'archived'")}
    shops = sorted((set(ProductSnapshotRepository.shop_ids_from_disk()) |
                    {int(shop['shopId']) for shop in configured if shop.get('shopId')}) - archived)
    if not shops:
        raise ValueError('没有配置店铺或本地店铺快照')
    return shops


def load_state(shops: list[int], *, all_products: bool = False,
               fresh: bool = False) -> tuple[dict, dict, list]:
    """保留显式 VC 操作与全店范围，按名称或 SKU 操作用已确认归属。"""
    book = MappingRepository.load_price_book(require_price=False, fresh=fresh)
    by_sku = {row['sku']: row for row in book}
    entries = db.vc_entries()
    shop_rows = {sid: load_shop_rows(sid) for sid in shops}
    observed = dict.fromkeys(vc for rows in shop_rows.values() if rows is not None for vc in rows)
    state = {}
    for vc in dict.fromkeys([*entries, *observed]):
        row = db.resolve_vc(vc, entries)
        if row['status'] == 'mapped':
            state[vc] = {**row, 'price_row': by_sku.get(row['sku'])}
        elif row['status'] == 'excluded' or (all_products and vc in observed):
            state[vc] = {**row, 'cn': ''}
    return state, shop_rows, book


def selection_error(vc: str, state: dict) -> str | None:
    row = state.get(vc, {})
    if row.get('status') == 'excluded':
        return '该商品已标记为非货盘'
    return None


def resolve_filters(args, state: dict, boss: list) -> tuple[list[str], str]:
    if getattr(args, 'vc', None):
        vcs = list(dict.fromkeys(v.strip() for v in args.vc.split(',') if v.strip()))
        return vcs, f'完整 VC（{len(vcs)} 个）'

    sku = getattr(args, 'sku', None)
    name = getattr(args, 'name', None)
    contains = getattr(args, 'name_contains', None)
    prefix = getattr(args, 'prefix', None)
    if not (sku or name or contains or prefix):
        return sorted(vc for vc, row in state.items() if row.get('status') != 'excluded'), '全部商品（保留人工排除）'

    def matches(row):
        cn = row.get('cn') or ''
        if sku:
            return row.get('sku') == sku
        if prefix:
            return str(row.get('prefix') or '').upper() == prefix.upper()
        return cn == name if name else contains in cn

    vcs = [vc for vc, row in state.items() if row.get('status') != 'excluded'
           and (extract_vc_prefix(vc) == prefix.upper() if prefix else matches(row))]
    labels = [f"{row['cn']}({row['sku']})" for row in boss if matches(row)]
    return sorted(vcs), ' / '.join(labels) or f'商品筛选 {sku or name or contains or prefix}'

"""按目标店铺、商品归属与显式条件选择商品。"""
from collections import defaultdict
import re

from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.domain.vendor_codes import VC_PREFIX_RE, extract_vc_prefix


def load_shop_rows(sid):
    """读某店 JSON → {vc: row}（在架缓存）；文件缺失返回 None"""
    try:
        res = ProductSnapshotRepository.load_shop_rows(sid)
    except Exception as error:
        print(f'  [快照不可读] 店{sid}：{type(error).__name__}，该店目标将记录到跳过报告')
        return None
    if res is None:
        p = config.shop_json_path(sid)
        print(f"  [跳过] 店{sid} 无数据文件 {p}，可单独运行 sellerops catalog fetch")
    return res


def selected_shops(args):
    if args.shops:
        return list(dict.fromkeys(int(value.strip()) for value in args.shops.split(",")))
    configured = credentials.get().wb.get("shops", [])
    shops = sorted(set(ProductSnapshotRepository.shop_ids_from_disk()) |
                   {int(shop["shopId"]) for shop in configured if shop.get("shopId")})
    if not shops:
        raise ValueError("没有配置店铺或本地店铺快照")
    return shops


def load_state(shops: list[int], *, all_products: bool = False) -> tuple[dict, dict, list]:
    """读取本轮归属和价格表行，保留既有纠偏及排除。"""
    state, excluded = MappingRepository.load_mapping_state()
    book = MappingRepository.load_price_book(require_price=False)
    by_name, by_prefix = defaultdict(list), defaultdict(list)
    for entry in book:
        if entry.get("cn"):
            by_name[entry["cn"]].append(entry)
        if entry.get("prefix"):
            by_prefix[entry["prefix"].upper()].append(entry)

    resolve_cn, _ = MappingRepository.build_vc_resolver({})
    excluded_vcs = {str(vc).upper() for vc in (*excluded, *MappingRepository.load_vc_excluded())}
    state = {vc: item for vc, item in state.items() if vc.upper() not in excluded_vcs}
    historical = {vc.upper(): item for vc, item in state.items()}
    shop_rows = {sid: load_shop_rows(sid) for sid in shops}
    observed = dict.fromkeys(vc for rows in shop_rows.values() if rows is not None for vc in rows)
    for vc in dict.fromkeys([*state, *observed]):
        if vc.upper() in excluded_vcs:
            continue
        old = historical.get(vc.upper(), {})
        cn = old.get("cn") or resolve_cn(vc, "")
        code = re.fullmatch(VC_PREFIX_RE, vc.upper())
        prefix = extract_vc_prefix(vc) if cn else (code.group(1) if code else None)
        prefixed = by_prefix.get(prefix, [])
        candidates = by_name.get(cn, []) if cn else prefixed
        if cn and len(candidates) > 1:
            candidates = [row for row in candidates if row in prefixed] or candidates
        if not cn and len({row["cn"] for row in candidates}) == 1:
            cn = candidates[0]["cn"]
        if cn or candidates or all_products or old:
            state[vc] = {**old, "cn": cn or "", "price_row": candidates[0] if len(candidates) == 1 else None,
                         "candidates": candidates}
    return state, shop_rows, book


def selection_error(vc: str, state: dict) -> str | None:
    candidates = state.get(vc, {}).get("candidates", [])
    if len(candidates) > 1:
        skus = "、".join(str(row.get("sku") or "空") for row in candidates)
        return f"归属无法唯一确认，候选 SKU：{skus}"
    return None


def resolve_filters(args, state: dict, boss: list) -> tuple[list[str], str]:
    """依据已确认的价格表行筛选，保留可能命中但需阻止的歧义链接。"""
    if getattr(args, "vc", None):
        vcs = list(dict.fromkeys(v.strip() for v in args.vc.split(",") if v.strip()))
        return vcs, f"完整 VC（{len(vcs)} 个）"

    sku = getattr(args, "sku", None)
    name = getattr(args, "name", None)
    contains = getattr(args, "name_contains", None)
    prefix = getattr(args, "prefix", None)
    if not (sku or name or contains or prefix):
        return sorted(state), "全部商品（保留人工排除）"

    def matches(row: dict) -> bool:
        cn = row.get("cn") or ""
        if sku:
            return row.get("sku") == sku
        if prefix:
            return str(row.get("prefix") or "").upper() == prefix.upper()
        return cn == name if name else contains in cn

    vcs = []
    for vc, identity in state.items():
        if prefix and extract_vc_prefix(vc) != prefix.upper():
            continue
        candidates = identity.get("candidates", [])
        if candidates:
            if any(matches(row) for row in candidates):
                vcs.append(vc)
        elif matches(identity) or (prefix and extract_vc_prefix(vc) == prefix.upper()):
            vcs.append(vc)
    labels = [f"{row['cn']}({row['sku']})" for row in boss if matches(row)]
    return sorted(vcs), " / ".join(labels) or f"商品筛选 {sku or name or contains or prefix}"

"""按目标店铺、商品归属与显式条件选择商品。"""
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.domain.vendor_codes import extract_vc_prefix


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


def load_state(shops, *, all_products=False):
    """本次操作的归属视图；纳入快照中新商品，不改写映射表或历史归属。"""
    state, excluded = MappingRepository.load_mapping_state()
    boss = MappingRepository.load_price_book(require_price=False)
    prefix_map = {}
    for row in boss:
        prefix = str(row.get("prefix") or "").upper()
        if prefix:
            previous = prefix_map.setdefault(prefix, {"cn": row["cn"]})
            if previous["cn"] != row["cn"]:
                previous["cn"] = ""
    resolve_cn, _ = MappingRepository.build_vc_resolver(prefix_map)
    excluded_vcs = {str(vc).upper() for vc in (*excluded, *MappingRepository.load_vc_excluded())}
    state = {vc: item for vc, item in state.items() if vc.upper() not in excluded_vcs}
    by_cn = {item['cn']: item for item in boss if item.get('cn')}
    shop_rows = {sid: load_shop_rows(sid) for sid in shops}
    for rows in shop_rows.values():
        if rows is None:
            continue
        for vc in rows:
            if vc in state or vc.upper() in excluded_vcs:
                continue
            cn = resolve_cn(vc, '')
            if cn in by_cn or all_products:
                state[vc] = {'cn': cn or '', 'dp': by_cn.get(cn, {}).get('dp')}
    if not state and all_products and any(rows is None for rows in shop_rows.values()):
        raise ValueError("目标店铺快照缺失或不可读，无法枚举全部商品")
    return state, shop_rows, boss


def resolve_filters(args, state, boss):
    """将显式商品条件解析为完整 VC 清单及运营摘要。"""
    if getattr(args, "vc", None):
        vcs = list(dict.fromkeys(v.strip() for v in args.vc.split(",") if v.strip()))
        miss = [v for v in vcs if v not in state]
        if miss:
            print(f"[警告] 以下 vendorCode 不在映射表中：{miss}（仍将尝试执行，可能查不到 nmId）")
        return vcs, f"vc 列表({len(vcs)} 个)"

    if getattr(args, "sku", None) or getattr(args, "name", None) or getattr(args, "name_contains", None) or getattr(args, "prefix", None):
        matched_boss = []
        for b in boss:
            if getattr(args, "sku", None) and b["sku"] == args.sku:
                matched_boss.append(b)
            elif getattr(args, "name", None) and args.name == b["cn"]:
                matched_boss.append(b)
            elif getattr(args, "name_contains", None) and args.name_contains in (b["cn"] or ""):
                matched_boss.append(b)
            elif getattr(args, "prefix", None) and str(b.get("prefix") or "").upper() == args.prefix.upper():
                matched_boss.append(b)
        if not matched_boss:
            print(f"[错误] 商品价格表中未匹配到商品（sku={getattr(args, 'sku', None)} name={getattr(args, 'name', None)} prefix={getattr(args, 'prefix', None)}）")
            candidates = sorted({b["cn"] for b in boss if b.get("cn") and getattr(args, "name", None)
                                 and args.name in b["cn"]})
            raise ValueError("没有匹配的商品。" + ("相似名称：" + "、".join(candidates[:20]) if candidates else ""))
        cns = {b["cn"] for b in matched_boss}
        vcs = sorted(vc for vc, st in state.items() if st["cn"] in cns)
        if getattr(args, "prefix", None):
            vcs = [vc for vc in vcs if extract_vc_prefix(vc) == args.prefix.upper()]
        desc = " / ".join(f"{b['cn']}({b['sku']})" for b in matched_boss)
        if not vcs:
            raise ValueError(f"{desc} 没有可确认归属的商品链接")
        return vcs, f"商品价格表商品: {desc}"

    return sorted(state.keys()), "全部商品（已排除记录除外）"



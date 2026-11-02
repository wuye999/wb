"""从本地快照跨店复制，完整 VC 是跨店去重键。"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from sellerops.domain.models import PublishItem
from sellerops.features.catalog import products as catalog_products
from sellerops.features.listings import card_publish
from sellerops.features.listings.publish_details import resolve_publish_details
from sellerops.features.listings.publish_inputs import parse_cn_stock, stock_for
from sellerops.features.listings.publish_records import load_records
from sellerops.features.listings.publish_report import print_publish_preview
from sellerops.persistence.mappings import MappingRepository
from sellerops.settings import paths as config


def build_coverage(shops_data):
    """本地在架行按完整 VC 汇总，忽略回收站行。"""
    vc_shops, vc_rows = {}, {}
    for sid, rows in shops_data.items():
        for row in rows:
            vc = (row.get("vendorCode") or "").strip()
            if vc and not row.get("trashedAt"):
                vc_shops.setdefault(vc, set()).add(sid)
                vc_rows.setdefault(vc, {})[sid] = row
    return vc_shops, vc_rows, sorted(shops_data)


def pick_source(vc, vc_rows, sid_main):
    """按主店优先、店 ID 升序选择有价格的源行。"""
    rows = [(sid, row) for sid, row in vc_rows.get(vc, {}).items()
            if any(s.get("price") is not None for s in row.get("sizeList") or [])]
    return min(rows, key=lambda x: (x[0] != sid_main, x[0])) if rows else (None, None)


def replication_price(row: Dict[str, Any], cn: str, price_book: List[Dict[str, Any]],
                      price_source: str = "snapshot", multiplier: float = 1.0) -> float:
    """最低售价按明确列读取；计算后按分四舍五入，不取整。"""
    if price_source == "minimum":
        values = {str(it.get("minimum_price")) for it in price_book if it.get("cn") == cn}
        if len(values) != 1:
            raise ValueError(f"{cn}最低售价缺失或同名商品价格不一致")
        raw = next(iter(values))
    elif price_source == "snapshot":
        raw = next((s.get("price") for s in row.get("sizeList") or []
                    if s.get("price") is not None), None)
    else:
        raise ValueError(f"未知价格来源：{price_source}")
    try:
        amount, factor = Decimal(str(raw)), Decimal(str(multiplier))
        if not amount.is_finite() or not factor.is_finite() or amount <= 0 or factor <= 0:
            raise ValueError("价格与倍数必须为正有限数")
        value = (amount * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if value <= 0:
            raise ValueError("目标价格不足一分")
        return float(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"{cn}价格或倍数无效") from exc


def build_replication_items(
    shops_data: Dict[int, List[Dict[str, Any]]],
    mapping_state: Dict[str, Any],
    price_book: List[Dict[str, Any]],
    source_shops: Optional[List[int]] = None,
    target_shops: Optional[List[int]] = None,
    price_source: str = "snapshot",
    price_multiplier: float = 1.0,
    stock: int = 999,
    records: Optional[Dict[str, Any]] = None,
) -> Tuple[List[PublishItem], List[Dict[str, str]]]:
    """零网络生成复制候选；源店和目标店独立，缺快照时明确失败。"""
    sources = set(source_shops if source_shops is not None else shops_data)
    targets = set(target_shops if target_shops is not None else shops_data)
    unknown = (sources | targets) - set(shops_data)
    if unknown:
        raise ValueError(f"这些店铺缺少本地快照：{sorted(unknown)}")
    if not sources or not targets:
        raise ValueError("源店和目标店都不能为空")
    if isinstance(stock, bool) or not isinstance(stock, int) or stock < 0:
        raise ValueError("库存必须为非负整数")
    coverage, source_rows, _ = build_coverage({sid: shops_data[sid] for sid in sources})
    all_coverage, _, _ = build_coverage(shops_data)
    items, blocked = [], []
    for vc in sorted(coverage):
        missing = sorted(targets - all_coverage.get(vc, set()))
        missing = [sid for sid in missing if str(sid) not in ((records or {}).get(vc) or {})]
        if not missing:
            continue
        row_map = source_rows[vc]
        if price_source == "minimum":
            src_sid = min(row_map, key=lambda sid: (sid != config.MAIN_SHOP, sid))
            row = row_map[src_sid]
        else:
            src_sid, row = pick_source(vc, source_rows, config.MAIN_SHOP)
        mapped = mapping_state.get(vc) or {}
        nm = str(mapped.get("nmId") or "").strip()
        try:
            if row is None:
                raise ValueError("源店无价格")
            if not nm.isdigit() or int(nm) <= 0:
                raise ValueError("映射表缺有效WB商品码，不从VC尾段猜测")
            cn = mapped.get("cn") or ""
            price = replication_price(row, cn, price_book, price_source, price_multiplier)
            items.append(PublishItem(
                nm_id=int(nm), vendor_code=vc, price=price, cn=cn, stock=stock,
                target_shops=missing, title=row.get("title") or "",
                main_image=row.get("repImg") or "", images=row.get("images") or "",
                extra={"src_sid": src_sid, "subjectId": row.get("subjectId")},
            ))
        except ValueError as exc:
            blocked.append({"vc": vc, "reason": str(exc)})
    return items, blocked


def run(args: Any) -> int:
    shops_data, _ = catalog_products.load_all_shops()
    state, _ = MappingRepository.load_mapping_state()
    book = MappingRepository.load_price_book()
    parse_ids = lambda raw: [int(s.strip()) for s in raw.split(",") if s.strip()] if raw else None
    items, blocked = build_replication_items(
        shops_data, state, book,
        source_shops=parse_ids(getattr(args, "source_shops", "")),
        target_shops=parse_ids(getattr(args, "shops", "")),
        price_source=getattr(args, "price_source", "snapshot"),
        price_multiplier=getattr(args, "price_multiplier", 1.0),
        stock=getattr(args, "stock", 999), records=load_records(),
    )
    wanted = {s.strip() for s in getattr(args, "vc", "").split(",") if s.strip()}
    prefix = getattr(args, "prefix", "").upper()
    name = getattr(args, "name", "")
    def selected(vc: str) -> bool:
        return ((not wanted or vc in wanted)
                and (not prefix or vc.startswith(f"BCS-{prefix}-"))
                and (not name or name in (state.get(vc) or {}).get("cn", "")))
    items = [it for it in items if selected(it.vendor_code)]
    blocked = [b for b in blocked if selected(b["vc"])]
    if getattr(args, "limit", 0):
        items = items[:args.limit]
    overrides = parse_cn_stock(getattr(args, "cn_stock", ""))
    for it in items:
        it.stock = stock_for(it.cn, overrides, default=it.stock)
    for b in blocked:
        print(f"[阻止] {b['vc']}: {b['reason']}")
    print_publish_preview(items, sorted(shops_data))
    print(f"候选 {len(items)} 个 VC，目标商品卡 {sum(len(it.target_shops) for it in items)} 张，阻止 {len(blocked)} 个")
    if not getattr(args, "apply", False):
        print("[dry-run] 使用本地快照及提交记录；加 --apply 提交。")
        return 1 if blocked else 0
    resolved_items = []
    cache = {}
    for it in items:
        resolved, err = resolve_publish_details(it, state, card_cache=cache)
        if err or not resolved:
            blocked.append({"vc": it.vendor_code, "reason": err})
            print(f"[阻止] {it.vendor_code}: {err}")
        else:
            resolved_items.append(resolved)
    result = card_publish.execute_publish(resolved_items, apply=True,
                                         interval=getattr(args, "interval", 1.0))
    return 1 if blocked or result["fail"] else 0

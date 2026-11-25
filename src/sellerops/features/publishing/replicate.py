"""从本地快照跨店复制，保留完整 VC 并检查同源商品。"""
from collections import Counter
from collections.abc import Callable
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from sellerops.domain.models import PublishItem
from sellerops.features.catalog import products as catalog_products
from sellerops.features.publishing import card_publish
from sellerops.features.publishing.publish_details import resolve_publish_details
from sellerops.features.publishing.publish_inputs import parse_cn_stock, stock_for
from sellerops.features.publishing.publish_records import load_records, records_by_shop
from sellerops.features.publishing.publish_report import print_publish_preview
from sellerops.features.publishing.publish_skips import PublishSkipReport, TargetCoverage, target_skip
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository
from sellerops.settings import paths as config


def _source_rows(shops_data):
    vc_rows = {}
    for sid, rows in shops_data.items():
        for row in rows:
            vc = (row.get("vendorCode") or "").strip()
            if vc and not row.get("trashedAt"):
                vc_rows.setdefault(vc, {})[sid] = row
    return vc_rows


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
    restrictions: Optional[dict] = None,
    record_vcs: Optional[dict] = None,
    *,
    selected: Callable[[str], bool] | None = None,
    skip_report: PublishSkipReport | None = None,
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
    source_rows = _source_rows({sid: shops_data[sid] for sid in sources})
    coverage = {sid: TargetCoverage(shops_data[sid]) for sid in sorted(targets)}
    records = records or {}
    restrictions = restrictions or {}
    submitted = records_by_shop(records if record_vcs is None else record_vcs)
    items, blocked = [], []
    for vc in sorted(source_rows):
        if selected is not None and not selected(vc):
            continue
        missing = []
        for sid, index in coverage.items():
            skip = target_skip(sid, vc, index, submitted, records, restrictions)
            if skip is None:
                missing.append(sid)
            elif skip_report is not None:
                skip_report.rows.append(skip)
        if not missing:
            continue
        row_map = source_rows[vc]
        if price_source == "minimum":
            src_sid = min(row_map, key=lambda sid: (sid != config.MAIN_SHOP, sid))
            row = row_map[src_sid]
        else:
            src_sid, row = pick_source(vc, source_rows, config.MAIN_SHOP)
        mapped = mapping_state.get(vc) or {}
        nm = str((row or {}).get("nmId") or "").strip()
        try:
            if row is None:
                raise ValueError("源店无价格")
            if not nm.isdigit() or int(nm) <= 0:
                raise ValueError("\u6e90\u5e97\u5feb\u7167\u7f3a\u6709\u6548 WB \u5546\u54c1\u7801")
            cn = mapped.get("cn") or ""
            sku = mapped.get('sku')
            price_rows = [product for product in price_book if product.get('sku') == sku] if sku else price_book
            price = replication_price(row, cn, price_rows, price_source, price_multiplier)
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
    limit = getattr(args, 'limit', 0)
    if limit < 0:
        raise ValueError('--limit 必须为非负整数（0 表示不限）')
    shops_data, _ = catalog_products.load_all_shops(include_trash=True)
    state, _ = MappingRepository.load_mapping_state()
    book = MappingRepository.load_price_book(
        fresh=getattr(args, 'apply', False) and getattr(args, 'price_source', '') == 'minimum')
    parse_ids = lambda raw: [int(s.strip()) for s in raw.split(",") if s.strip()] if raw else None
    wanted = {s.strip() for s in (getattr(args, "vc", "") or "").split(",") if s.strip()}
    prefix = (getattr(args, "prefix", "") or "").upper()
    name = getattr(args, "name", "")
    contains = getattr(args, "name_contains", None)

    def selected(vc: str) -> bool:
        return ((not wanted or vc in wanted)
                and (not prefix or vc.startswith(f"BCS-{prefix}-"))
                and (not name or name == (state.get(vc) or {}).get("cn", ""))
                and (not contains or contains in (state.get(vc) or {}).get("cn", "")))
    report = PublishSkipReport('跨店复制')
    items, blocked = build_replication_items(
        shops_data, state, book,
        source_shops=parse_ids(getattr(args, "source_shops", "")),
        target_shops=parse_ids(getattr(args, "shops", "")),
        price_source=getattr(args, "price_source", "snapshot"),
        price_multiplier=getattr(args, "price_multiplier", 1.0),
        stock=getattr(args, "stock", 999), records=load_records(), restrictions=db.vc_entries(),
        record_vcs=load_records(vc_only=True), selected=selected, skip_report=report,
    )
    candidate_count = len(items)
    remaining_vcs = {it.vendor_code for it in items} | {row['vc'] for row in blocked}
    fully_skipped = {row.vc for row in report.rows} - remaining_vcs
    selected_count = len(remaining_vcs) + len(fully_skipped)
    initial_skips = len(report.rows)
    if limit:
        items = items[:limit]
    overrides = parse_cn_stock(getattr(args, "cn_stock", ""))
    for it in items:
        it.stock = stock_for(it.cn, overrides, default=it.stock)
    for b in blocked:
        print(f"[阻止] {b['vc']}: {b['reason']}")
    candidate_targets = sum(len(it.target_shops) for it in items)
    print(f"筛选命中 {selected_count} 个源 VC，限额前候选 {candidate_count} 个，"
          f"本次候选 {len(items)} 个，目标商品卡 {candidate_targets} 张，限额裁剪 {candidate_count - len(items)} 个")
    capture = getattr(args, "capture_plan", False)
    apply = getattr(args, 'apply', False)
    if not apply and not capture:
        print_publish_preview(items, sorted(shops_data))
        print("[dry-run] 使用本地快照及提交记录；加 --apply 提交。")
        report.write()
        result = {'fail': 0, 'prepared_targets': 0}
    else:
        resolved_items = []
        cache = {}
        for it in items:
            resolved, err = resolve_publish_details(it, state, card_cache=cache)
            if err or not resolved:
                blocked.append({"vc": it.vendor_code, "reason": err})
                print(f"[阻止] {it.vendor_code}: {err}")
            else:
                resolved_items.append(resolved)
        result = card_publish.execute_publish(resolved_items, apply=apply, skip_report=report,
                                              interval=getattr(args, "interval", 1.0), capture=capture)
        additional = Counter(row.vc for row in report.rows[initial_skips:])
        fully_skipped.update(it.vendor_code for it in resolved_items
                             if additional[it.vendor_code] == len(it.target_shops))
        blocked.extend({'vc': row['vc'], 'reason': row['msg']} for row in result['details'] if not row['ok'])
    if result.get('prepared'):
        args.prepared = result['prepared']
    replication = {'selected_vcs': selected_count, 'candidate_vcs_before_limit': candidate_count,
                   'candidate_vcs': len(items), 'candidate_targets': candidate_targets,
                   'limited_vcs': candidate_count - len(items), 'prepared_targets': result['prepared_targets'],
                   'blocked_vcs': len({row['vc'] for row in blocked}), 'fully_skipped_vcs': len(fully_skipped),
                   **report.summary()}
    print(f"最终待提交 {replication['prepared_targets']} 张，阻止 {replication['blocked_vcs']} 个 VC，"
          f"跳过 {replication['skipped_targets']} 个目标，全部目标跳过 {len(fully_skipped)} 个 VC")
    args.outcome = {'replication': replication}
    if report.path:
        args.outcome['artifacts'] = {'skips_csv': str(report.path)}
    if result.get('daily_limit_warnings'):
        args.outcome['daily_limit_warnings'] = result['daily_limit_warnings']
    return 1 if blocked or result["fail"] else 0

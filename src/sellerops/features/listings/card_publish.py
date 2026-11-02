"""保留完整供应商代码的建卡发布。"""
import copy
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sellerops.core import utils as common
from sellerops.domain.models import PublishItem
from sellerops.integrations.bcs import client as bcs
from sellerops.persistence.mappings import MappingRepository
from sellerops.features.listings.warehouse import resolve_warehouse
from sellerops.features.listings.publish_inputs import (
    DEFAULT_STOCK, load_publish_file, parse_cn_stock, parse_dims_str, stock_for,
)
from sellerops.features.listings.publish_records import load_records, save_records, record_publish_success
from sellerops.features.listings.publish_details import resolve_publish_details, fetch_wb_detail_bcs
from sellerops.features.listings.publish_report import create_publish_csv, print_publish_preview
from sellerops.features.listings.product_details import fetch_card_json, price_book_packages
from sellerops.features.listings.card_payload import build_card_payload


def execute_publish(
    items: List[PublishItem],
    apply: bool = False,
    interval: float = 1.0,
    overrides: Optional[Dict[str, int]] = None,
    csv_prefix: str = "建卡上架",
    writer: Optional[Any] = None,
    csv_path: Optional[str] = None,
    row_formatter: Optional[Any] = None,
    check_exists: bool = True,
    warehouses: Optional[Dict[int, int]] = None,
) -> Dict[str, Any]:
    """保留完整 VC 建卡；先完成整批读取和载荷准备，再提交写入。

    check_exists 默认使用本地全状态快照及提交记录，不联网扫描商品列表。
    warehouses 可传入本次执行前已经确认的各店莫斯科仓 ID，避免重复读取。
    ok/skip/fail 按商品组计数；details 保留各组目标店与受理结果。
    """
    from sellerops.persistence.snapshots import ProductSnapshotRepository

    common.validate_batch_options(interval=interval)
    result = {"ok": 0, "skip": 0, "fail": 0, "plans": len(items),
              "csv": csv_path or "", "details": []}
    if not items:
        return result
    explicit = {sid for it in items for sid in (it.target_shops or [])}
    if any(it.target_shops is None for it in items):
        all_shop_ids = [s["id"] for s in bcs.fetch_shop_list() if s.get("id")]
    else:
        all_shop_ids = sorted(explicit)
    target_ids = explicit | {sid for it in items if it.target_shops is None for sid in all_shop_ids}
    if warehouses is None:
        warehouses = {sid: wh for sid in sorted(target_ids)
                      if (wh := resolve_warehouse(sid)) is not None}
    records = load_records()
    existing = {}
    if check_exists:
        for sid in sorted(target_ids):
            if not ProductSnapshotRepository.exists(sid):
                raise ValueError(f"店{sid}缺少本地快照，不能把未知商品清单当作空店")
            existing[sid] = {r.get("vendorCode") for r in ProductSnapshotRepository.load_shop_products(sid)}

    prepared = []
    seen = set()
    # 所有网络读取必须在首个写请求之前结束。
    for it in items:
        targets = list(dict.fromkeys(it.target_shops if it.target_shops is not None else all_shop_ids))
        invalid = [sid for sid in targets if not warehouses.get(sid)]
        if not targets or invalid:
            result["fail"] += 1
            result["details"].append({"vc": it.vendor_code, "targets": targets,
                                      "ok": False, "msg": f"目标店或莫斯科仓不可用：{invalid}"})
            continue
        final_targets = []
        for sid in targets:
            pair = (it.vendor_code, sid)
            submitted = str(sid) in (records.get(it.vendor_code) or {})
            if pair in seen or (check_exists and (submitted or it.vendor_code in existing[sid])):
                continue
            final_targets.append(sid)
            seen.add(pair)
        if not final_targets:
            result["skip"] += 1
            continue
        card_info = it.extra.get("card_info") or fetch_card_json(it.nm_id)
        if not card_info:
            result["fail"] += 1
            result["details"].append({"vc": it.vendor_code, "targets": final_targets,
                                      "ok": False, "msg": "card.json获取失败"})
            continue
        # 已缓存详情（包括合成详情）无需再次请求。
        detail = it.extra.get("wb_detail") or fetch_wb_detail_bcs(it.nm_id)
        payload, err = build_card_payload(it, final_targets, warehouses, card_info, detail, overrides)
        if err or not payload:
            result["fail"] += 1
            result["details"].append({"vc": it.vendor_code, "targets": final_targets,
                                      "ok": False, "msg": err or "载荷构造失败"})
            continue
        prepared.append((it, final_targets, payload))
    for d in result["details"]:
        print(f"[失败] {d['vc']}: {d['msg']}")
    if not apply:
        preview = []
        for it, targets, _ in prepared:
            cp = copy.copy(it)
            cp.target_shops = targets
            preview.append(cp)
        print_publish_preview(preview, all_shop_ids)
        print(f"[dry-run] 待提交 {len(prepared)} 组，跳过 {result['skip']}，失败 {result['fail']}。")
        return result

    managed_file = writer is None
    csv_file = None
    if managed_file:
        csv_file, writer, csv_path = create_publish_csv(csv_prefix)
        result["csv"] = csv_path
    try:
        for idx, (it, targets, payload) in enumerate(prepared, 1):
            now = datetime.now().strftime("%H:%M:%S")
            target_str = ",".join(map(str, targets))
            accepted = False
            response = None
            try:
                response = bcs.client.post(f"{bcs.client.base_url}/system/wbCollection/wb/new", payload)
                accepted = response.get("code") == 200
                msg = "" if accepted else (response.get("msg") or f"code={response.get('code')}")
            except Exception as exc:
                msg = f"提交异常，受理状态未知，不自动重放：{type(exc).__name__}: {exc}"
            status = "已受理" if accepted else "失败"
            result["ok" if accepted else "fail"] += 1
            result["details"].append({"vc": it.vendor_code, "targets": targets,
                                      "ok": accepted, "msg": msg, "response": response})
            if accepted:
                record_publish_success(records, it, targets)
                save_records(records)
            stock = stock_for(it.cn, overrides, default=it.stock)
            if row_formatter:
                row = row_formatter({"time": now, "item": it, "targets_str": target_str,
                                     "stock": stock, "status": status, "msg": msg})
            else:
                row = [now, it.nm_id, it.vendor_code, it.cn, f"{float(it.price):.2f}",
                       it.length, it.width, it.height, it.weight, target_str, stock, status, msg]
            writer.writerow(row)
            if csv_file:
                csv_file.flush()
            print(f"[{idx}/{len(prepared)}] {status} {it.vendor_code} → [{target_str}] {msg}")
            if idx < len(prepared):
                time.sleep(interval)
    finally:
        if csv_file:
            csv_file.close()
    print(f"[汇总] 已受理 {result['ok']} | 跳过 {result['skip']} | 失败 {result['fail']}")
    if result["ok"] and managed_file:
        common.print_write_hint()
    return result

def publish_product(
    nm_id: int,
    price: Optional[float] = None,
    vendor_code: Optional[str] = None,
    prefix: Optional[str] = None,
    dims: Optional[str] = None,
    shops: Optional[List[int]] = None,
    stock: int = DEFAULT_STOCK,
    cn: str = "",
    apply: bool = False,
) -> Dict[str, Any]:
    """单商品快速上架便捷函数（旧接口）"""
    dim_tuple = parse_dims_str(dims) if dims else None
    item = PublishItem(
        nm_id=nm_id,
        price=price,
        vendor_code=vendor_code,
        prefix=prefix,
        length=dim_tuple[0] if dim_tuple else None,
        width=dim_tuple[1] if dim_tuple else None,
        height=dim_tuple[2] if dim_tuple else None,
        weight=dim_tuple[3] if dim_tuple else None,
        stock=stock,
        target_shops=shops,
        cn=cn,
    )
    resolved, err = resolve_publish_details(item)
    if err or not resolved:
        print(f"[参数错误] {err}")
        return {"ok": 0, "skip": 0, "fail": 1, "plans": 1, "csv": ""}
    return execute_publish([resolved], apply=apply)

def run(args: Any) -> int:
    """CLI 入口逻辑"""
    raw_nms: List[str] = []
    pos_nms = getattr(args, "nms", None) or []
    if isinstance(pos_nms, str):
        pos_nms = [pos_nms]
    for p in pos_nms:
        for sub in str(p).replace(",", " ").split():
            sub = sub.strip()
            if sub and sub.isdigit():
                raw_nms.append(sub)

    opt_nm = getattr(args, "nm", "")
    if opt_nm:
        for sub in str(opt_nm).replace(",", " ").split():
            sub = sub.strip()
            if sub and sub.isdigit():
                raw_nms.append(sub)

    file_path = getattr(args, "file", "")
    file_items: List[Dict[str, Any]] = []
    if file_path and os.path.exists(file_path):
        f_items, f_nms = load_publish_file(file_path)
        file_items.extend(f_items)
        raw_nms.extend(f_nms)

    seen_nm = set()
    unique_nms = []
    for nm in raw_nms:
        if nm not in seen_nm:
            seen_nm.add(nm)
            unique_nms.append(int(nm))

    if not unique_nms and not file_items:
        print("[错误] 未指定任何 WB 商品码，请传入商品码或 --file 文件。")
        return 1

    allow_shops = (
        [int(x) for x in args.shops.split(",") if x.strip()]
        if getattr(args, "shops", "")
        else None
    )

    dims_tuple = parse_dims_str(getattr(args, "dims", ""))
    length = dims_tuple[0] if dims_tuple else getattr(args, "length", None)
    width = dims_tuple[1] if dims_tuple else getattr(args, "width", None)
    height = dims_tuple[2] if dims_tuple else getattr(args, "height", None)
    weight = dims_tuple[3] if dims_tuple else getattr(args, "weight", None)

    raw_vc = getattr(args, "vc", "").strip()
    vc_list = [v.strip() for v in raw_vc.split(",") if v.strip()] if "," in raw_vc else []

    mapping_state, _ = MappingRepository.load_mapping_state()
    pmap = MappingRepository.load_prefix_map()
    boss_pkg = price_book_packages()
    card_cache: Dict[int, Any] = {}

    shelve_items: List[PublishItem] = []

    for f_it in file_items:
        f_nm = int(f_it.get("nmId") or f_it.get("sku") or f_it.get("nm"))
        shelve_items.append(PublishItem(
            nm_id=f_nm,
            price=f_it.get("price") if f_it.get("price") is not None else getattr(args, "price", None),
            vendor_code=f_it.get("vendorCode") or f_it.get("vc"),
            prefix=f_it.get("prefix") or getattr(args, "prefix", None),
            length=f_it.get("length") or length,
            width=f_it.get("width") or width,
            height=f_it.get("height") or height,
            weight=f_it.get("weight") or weight,
            stock=f_it.get("stock") if f_it.get("stock") is not None else getattr(args, "stock", DEFAULT_STOCK),
            target_shops=allow_shops or f_it.get("targetShops"),
            cn=f_it.get("cn") or getattr(args, "cn", ""),
            title=f_it.get("title", ""),
            main_image=f_it.get("mainImage", ""),
            images=f_it.get("images", ""),
            extra={
                **(f_it.get("extra") or {}),
                "orig_vc": f_it.get("orig_vc"),
                "orig_nm": f_it.get("orig_nm"),
                "raw_cur_nm": f_it.get("raw_cur_nm"),
            }
        ))

    for idx, nm in enumerate(unique_nms):
        item_vc = None
        if raw_vc:
            if "{nm}" in raw_vc:
                item_vc = raw_vc.format(nm=nm)
            elif vc_list and idx < len(vc_list):
                item_vc = vc_list[idx]
            elif len(unique_nms) == 1:
                item_vc = raw_vc

        shelve_items.append(PublishItem(
            nm_id=nm,
            price=getattr(args, "price", None),
            vendor_code=item_vc,
            prefix=getattr(args, "prefix", None),
            length=length,
            width=width,
            height=height,
            weight=weight,
            stock=getattr(args, "stock", DEFAULT_STOCK),
            target_shops=allow_shops,
            cn=getattr(args, "cn", ""),
        ))

    resolved_items: List[PublishItem] = []
    resolution_failures = 0
    for it in shelve_items:
        resolved, err = resolve_publish_details(it, mapping_state, pmap, boss_pkg, card_cache)
        if err or not resolved:
            resolution_failures += 1
            print(f"[跳过] 商品 {it.nm_id}: {err}")
            continue
        resolved_items.append(resolved)

    if not resolved_items:
        print("[错误] 没有可执行的有效上架任务（全部商品信息校验失败）。")
        return 1

    overrides = parse_cn_stock(getattr(args, "cn_stock", "") or "")
    res = execute_publish(
        resolved_items,
        apply=getattr(args, "apply", False),
        interval=getattr(args, "interval", 1.0),
        overrides=overrides,
    )
    return 1 if resolution_failures or res["fail"] else 0

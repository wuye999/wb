"""保留完整供应商代码的建卡发布。"""
import copy
from collections import Counter
from dataclasses import asdict, replace
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sellerops.core import utils as common
from sellerops.domain.models import PublishItem
from sellerops.integrations.bcs.client import client as bcs
from sellerops.persistence import db
from sellerops.features.products.warehouse import cached_warehouse_name, resolve_warehouse
from sellerops.features.publishing.publish_inputs import (
    DEFAULT_STOCK, load_publish_file, parse_cn_stock, parse_dims_str, publish_codes, stock_for,
)
from sellerops.features.publishing.publish_records import (
    load_records, publish_keys, publish_task_id, publish_vc_key, records_by_shop,
    submit_once, today_publish_counts, vendor_code_index,
)
from sellerops.features.publishing.publish_details import resolve_publish_details, resolve_publish_items
from sellerops.features.publishing.publish_report import create_publish_csv, print_publish_preview
from sellerops.features.publishing.publish_skips import PublishSkipReport, TargetCoverage, target_skip
from sellerops.features.publishing.product_details import fetch_card_json
from sellerops.features.publishing.card_payload import build_card_payload


def _matching_target(sid: int, vc: str, existing: dict, submitted_vcs: dict) -> bool:
    key = publish_vc_key(vc)
    matched = existing.get(sid, {}).get(key) or submitted_vcs.get(str(sid), {}).get(key)
    if matched:
        print(f"[跳过] 店铺 {sid} 的 {vc} 命中目标店已有同源商品 VC：{matched}")
    return bool(matched)


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
    capture: bool = False,
    frozen: dict | None = None,
    limit: int = 0,
    skip_report: PublishSkipReport | None = None,
) -> Dict[str, Any]:
    """保留完整 VC 建卡；先完成整批读取和载荷准备，再提交写入。

    check_exists 默认使用本地全状态快照及提交记录，不联网扫描商品列表。
    warehouses 可传入本次执行前已经确认的各店莫斯科仓 ID，避免重复读取。
    ok/skip/fail 按商品组计数；details 保留各组目标店与受理结果。
    """
    from sellerops.persistence.snapshots import ProductSnapshotRepository

    common.validate_batch_options(interval=interval)
    if frozen is not None:
        result = {'ok': 0, 'skip': 0, 'fail': 0, 'plans': len(frozen['items']), 'csv': '', 'details': [],
                  'daily_limit_warnings': []}
        overrides = frozen['overrides']
        submitted_vcs = records_by_shop(load_records(vc_only=True))
        target_ids = {sid for _, targets, _ in frozen['items'] for sid in targets}
        existing = {
            sid: vendor_code_index(row.get('vendorCode') for row in ProductSnapshotRepository.load_shop_products(sid))
            for sid in sorted(target_ids) if check_exists and ProductSnapshotRepository.exists(sid)
        }
        prepared = []
        for fields, targets, payload in frozen['items']:
            it = PublishItem(**fields)
            final_targets = [sid for sid in targets if not _matching_target(sid, it.vendor_code, existing, submitted_vcs)]
            if not final_targets:
                result['skip'] += 1
                continue
            if final_targets != targets:
                shops = [shop for shop in json.loads(payload['shop']) if shop['id'] in final_targets]
                payload = {**payload, 'shop': db.dumps(shops)}
            prepared.append((it, final_targets, payload))
    else:
        result = {"ok": 0, "skip": 0, "fail": 0, "plans": len(items),
                  "csv": csv_path or "", "details": [], "daily_limit_warnings": [],
                  'scanned_valid': 0, 'total_valid': len(items), 'limit_reached_early': False,
                  'prepared_targets': 0}
        if not items:
            if skip_report is not None:
                skip_report.write()
            return result
        explicit = {sid for it in items for sid in (it.target_shops or [])}
        if any(it.target_shops is None for it in items):
            all_shop_ids = [s["id"] for s in bcs.fetch_shop_list() if s.get("id")]
        else:
            all_shop_ids = sorted(explicit)
        target_ids = explicit | {sid for it in items if it.target_shops is None for sid in all_shop_ids}
        if capture:
            from sellerops.features.catalog.shops import archive_set
            archived = archive_set()
            target_ids -= archived
            items = [it for it in items if it.target_shops is None or set(it.target_shops).isdisjoint(archived)]
            all_shop_ids = [sid for sid in all_shop_ids if sid not in archived]
        if warehouses is None:
            warehouses = {sid: wh for sid in sorted(target_ids)
                          if (wh := resolve_warehouse(sid)) is not None}
        records = load_records()
        submitted_vcs = records_by_shop(load_records(vc_only=True))
        restrictions = db.vc_entries()
        existing = {}
        if check_exists:
            for sid in sorted(target_ids):
                if not ProductSnapshotRepository.exists(sid):
                    raise ValueError(f"店{sid}缺少本地快照，不能把未知商品清单当作空店")
                existing[sid] = TargetCoverage(ProductSnapshotRepository.load_shop_products(sid))

        prepared = []
        seen = set()
        # 所有网络读取必须在首个写请求之前结束。
        for scanned, it in enumerate(items, 1):
            result['scanned_valid'] = scanned
            targets = list(dict.fromkeys(it.target_shops if it.target_shops is not None else all_shop_ids))
            invalid = [sid for sid in targets if not warehouses.get(sid)]
            if not targets or invalid:
                result["fail"] += 1
                result["details"].append({"vc": it.vendor_code, "targets": targets,
                                          "ok": False, "msg": f"目标店或默认业务仓不可用：{invalid}"})
                continue
            final_targets = []
            vc_key = publish_vc_key(it.vendor_code)
            for sid in targets:
                skip = target_skip(sid, it.vendor_code, existing.get(sid), submitted_vcs, records, restrictions,
                                   keys=publish_keys(it), duplicate=(vc_key, sid) in seen)
                if skip:
                    if skip_report is not None:
                        skip_report.rows.append(replace(skip, line=it.extra.get('table_line')))
                    elif skip.matched_vc:
                        print(f'[跳过] 店铺 {sid} 的 {it.vendor_code} 命中目标店已有同源商品 VC：{skip.matched_vc}')
                    continue
                final_targets.append(sid)
            if not final_targets:
                result["skip"] += 1
                continue
            card_info = it.extra.get("card_info") or fetch_card_json(it.nm_id)
            if not card_info:
                result["fail"] += 1
                result["details"].append({"vc": it.vendor_code, "targets": final_targets,
                                          "ok": False, "msg": "card.json获取失败"})
                continue
            detail = it.extra.get("wb_detail")
            payload, err = build_card_payload(it, final_targets, warehouses, card_info, detail, overrides)
            if err or not payload:
                result["fail"] += 1
                result["details"].append({"vc": it.vendor_code, "targets": final_targets,
                                          "ok": False, "msg": err or "载荷构造失败"})
                continue
            prepared.append((it, final_targets, payload))
            seen.update((vc_key, sid) for sid in final_targets)
            if limit > 0 and len(prepared) >= limit:
                result['limit_reached_early'] = scanned < len(items)
                break
        result['prepared_targets'] = sum(len(targets) for _, targets, _ in prepared)
        if skip_report is not None:
            skip_report.write()
            scanned, total = result['scanned_valid'], result['total_valid']
            if result['limit_reached_early']:
                print(f'已扫描前 {scanned} / 共 {total} 条有效商品（达到 --limit 提前停止，另有 {total - scanned} 条未扫描）')
            elif limit:
                print(f'已扫描全部 {scanned} / 共 {total} 条有效商品')
        for d in result["details"]:
            print(f"[失败] {d['vc']}: {d['msg']}")

    if prepared:
        today = today_publish_counts()
        pending_by_shop = Counter(sid for _, targets, _ in prepared for sid in targets)
        for sid, pending in sorted(pending_by_shop.items()):
            submitted = today.get(sid, 0)
            total = submitted + pending
            if total > 1000:
                result["daily_limit_warnings"].append({"shop_id": sid, "today_submitted": submitted,
                                                        "pending": pending, "total": total})
                print(f"[警告] 店铺 {sid} 今日已提交 {submitted} 张，本次待提交 {pending} 张，合计 {total} 张，"
                      "超过每日 1000 张建卡限额，建议分天或分批上架。")

    if frozen is None and not apply:
        preview = []
        for it, targets, _ in prepared:
            cp = copy.copy(it)
            cp.target_shops = targets
            preview.append(cp)
        business_warehouses = ({sid: (wid, cached_warehouse_name(sid, wid)) if (wid := warehouses.get(sid)) else None
                                for sid in target_ids} if skip_report is not None else None)
        print_publish_preview(preview, all_shop_ids, warehouses=business_warehouses)
        print(f"[dry-run] 待提交 {len(prepared)} 组，跳过 {result['skip']}，失败 {result['fail']}。")
        if capture and prepared:
            result['prepared'] = {'items': [(asdict(it), targets, payload) for it, targets, payload in prepared],
                                  'warehouses': {sid: warehouses[sid] for _, targets, _ in prepared for sid in targets},
                                  'overrides': overrides, 'blocked': result['details']}
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
                response = submit_once([it], targets,
                                       lambda: bcs.post(f"{bcs.base_url}/system/wbCollection/wb/new", payload))
                accepted = response.get("code") == 200
                msg = "" if accepted else (response.get("msg") or f"code={response.get('code')}")
            except Exception as exc:
                msg = f"提交异常，受理状态未知，不自动重放：{type(exc).__name__}: {exc}"
            status = "已受理" if accepted else "失败"
            result["ok" if accepted else "fail"] += 1
            result["details"].append({"vc": it.vendor_code, "targets": targets,
                                      "ok": accepted, "msg": msg, "response": response,
                                      'task_id': publish_task_id(response) if isinstance(response, dict) else None,
                                      'status': 'accepted' if accepted else 'failed'
                                      if isinstance(response, dict) and type(response.get('code')) is int else 'unknown',
                                      'mapping_conflict': isinstance(response, dict)
                                      and it.vendor_code in response.get('mapping_conflicts', [])})
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


def publish_cards_simple(
    items: list[dict | PublishItem],
    apply: bool = False,
    interval: float = 0.5,
    limit: int = 0,
    check_exists: bool = True,
    *,
    skip_report: PublishSkipReport | None = None,
) -> dict[str, Any]:
    """用直接提供的商品字段预览或提交独立 BCS 建卡。"""
    required = ('nm_id', 'vendor_code', 'price', 'length', 'width', 'height', 'weight', 'target_shops')
    candidates = []
    for raw in items:
        if isinstance(raw, PublishItem):
            item = raw
        else:
            values = dict(raw)
            card_info = values.pop('card_info', None)
            if card_info is not None:
                values['extra'] = {**(values.get('extra') or {}), 'card_info': card_info}
            if 'nm_id' not in values:
                raise ValueError('建卡商品缺少必填字段 nm_id')
            item = PublishItem(**values)
        missing = [field for field in required if getattr(item, field) is None]
        if missing:
            raise ValueError(f"建卡商品缺少必填字段：{'、'.join(missing)}")
        if type(item.nm_id) is not int or item.nm_id <= 0:
            raise ValueError('nm_id 必须是正整数')
        if not item.target_shops or any(type(sid) is not int or sid <= 0 for sid in item.target_shops):
            raise ValueError('target_shops 必须提供目标店铺 ID')
        candidates.append(item)
    return execute_publish(candidates, apply=apply, interval=interval, limit=limit, check_exists=check_exists,
                           skip_report=skip_report)


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
        args.summary = "未指定任何 WB 商品码，请传入商品码或 --file 文件"
        print(f"[错误] {args.summary}")
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

    file_nms = [int(row.get('nmId') or row.get('sku') or row.get('nm')) for row in file_items]
    codes = publish_codes(getattr(args, 'vc', ''), file_nms + unique_nms)

    shelve_items: List[PublishItem] = []

    for f_it, f_nm, code in zip(file_items, file_nms, codes[:len(file_items)], strict=True):
        shelve_items.append(PublishItem(
            nm_id=f_nm,
            price=f_it.get("price") if f_it.get("price") is not None else getattr(args, "price", None),
            vendor_code=f_it.get("vendorCode") or f_it.get("vc") or code,
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
                "source_tail": f_it.get("source_tail"),
                "seller_sku": str(f_it.get("sellerSku") or getattr(args, "sku", "")).strip(),
                "random_prefix": f_it.get("randomPrefix") is True or getattr(args, "random_prefix", False),
                "raw_cur_nm": f_it.get("raw_cur_nm"),
            }
        ))

    for nm, item_vc in zip(unique_nms, codes[len(file_items):], strict=True):
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
            extra={"seller_sku": getattr(args, "sku", ""), "random_prefix": getattr(args, "random_prefix", False)},
        ))

    resolved_items, resolution_failures = resolve_publish_items(args, shelve_items, bulk=False)

    if not resolved_items:
        print("[错误] 没有可执行的有效上架任务（全部商品信息校验失败）。")
        return 1

    overrides = parse_cn_stock(getattr(args, "cn_stock", "") or "")
    res = execute_publish(
        resolved_items,
        apply=getattr(args, "apply", False),
        interval=getattr(args, "interval", 1.0),
        overrides=overrides,
        **({'capture': True} if getattr(args, 'capture_plan', False) else {}),
    )
    if isinstance(res, dict) and res.get('prepared'):
        args.prepared = res['prepared']
    if res.get('daily_limit_warnings'):
        args.outcome = {'daily_limit_warnings': res['daily_limit_warnings']}
    return 1 if resolution_failures or res["fail"] else 0

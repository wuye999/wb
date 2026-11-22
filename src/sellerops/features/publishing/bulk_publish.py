"""
sellerops 批量发布上架服务 (bulk_publish)

使用 BCS 新版批量上品接口 (POST /products/batch/push) 实现商品自动上架。
通过统一 CLI 运行，支持单商品/多商品批量、前缀码、价格、尺寸重量与目标店铺；完整自定义 VC 需要建卡协议。
同时作为跨店复制 (replicate) 与他人映射表导入 (import-publish) 的底层上架引擎。

用法示例：
  python run.py ops publish 123456789 --shops 1001 --sku SKU-1 --price 59 --stock 20
  python run.py ops publish 123456789 --shops 1001 --sku SKU-1 --prefix TEST --dims "10*20*30/0.5" --stock 20
  python run.py ops create-cards 123456789 --vc BCS-TEST-123456789 --shops 1001 --sku SKU-1 --price 59 --stock 20
"""
from sellerops.features.products.warehouse import resolve_warehouse


import copy
from dataclasses import asdict


import os

import time
from datetime import datetime

from typing import Dict, Any, List, Optional, Tuple


from sellerops.domain.models import PublishItem
from sellerops.integrations.bcs.client import client as bcs
from sellerops.persistence import db


from sellerops.core import utils as common

from sellerops.features.publishing.publish_inputs import DEFAULT_STOCK

from sellerops.features.publishing.publish_records import load_records
from sellerops.features.publishing.publish_records import publish_keys, publish_task_id, submit_once
from sellerops.features.publishing.publish_records import vc_exists_in_shop
from sellerops.features.publishing.publish_inputs import parse_dims_str
from sellerops.features.publishing.publish_inputs import parse_cn_stock
from sellerops.features.publishing.publish_inputs import stock_for
from sellerops.features.publishing.publish_details import resolve_publish_details, resolve_publish_items
from sellerops.features.publishing.publish_report import create_publish_csv
from sellerops.features.publishing.publish_report import print_publish_preview
from sellerops.features.publishing.publish_inputs import load_publish_file, publish_codes

BATCH_SIZE = 50


def execute_publish(
    items: List[PublishItem],
    apply: bool = False,
    interval: float = 1.0,
    overrides: Optional[Dict[str, int]] = None,
    csv_prefix: str = "新接口上架",
    writer: Optional[Any] = None,
    csv_path: Optional[str] = None,
    row_formatter: Optional[Any] = None,
    check_exists: bool = True,
    capture: bool = False,
    frozen: dict | None = None,
) -> Dict[str, Any]:
    """
    使用新版批量接口 (POST /products/batch/push) 执行商品上架。

    Args:
        items: 已补全信息的 PublishItem 实体列表。
        apply: True 为真正调用平台接口执行；False 为 dry-run 预览。
        interval: 批次间请求等待秒数。
        overrides: 中文名→库存覆盖字典。
        csv_prefix: 输出 CSV 日志文件名前缀。
        writer: 外部传入的 csv.writer（为 None 时内部自动创建）。
        csv_path: 外部传入的 CSV 文件路径。
        row_formatter: 自定义 CSV 行格式化回调。
        check_exists: 是否在线二次查重（调用方已查重时可设为 False）。

    Returns:
        执行统计字典: {"ok": int, "skip": int, "fail": int, "plans": len(items), "csv": str}
    """
    common.validate_batch_options(interval=interval)
    if frozen is not None:
        valid_items = [{**row, 'item': PublishItem(**row['item'])} for row in frozen['items']]
        items = [row['item'] for row in valid_items]
        warehouses = {int(sid): wh for sid, wh in frozen['warehouses'].items()}
        skip_count = fail_count = 0
    else:
        # 1. 获取目标店铺与仓库
        all_shops = bcs.fetch_shop_list()
        all_shop_ids = [s["id"] for s in all_shops if s.get("id")]
        if capture:
            from sellerops.features.catalog.shops import archive_set
            archived = archive_set()
            all_shop_ids = [sid for sid in all_shop_ids if sid not in archived]
        if not all_shop_ids:
            print("[错误] 未获取到可用店铺列表")
            return {"ok": 0, "skip": 0, "fail": len(items), "plans": len(items), "csv": ""}

        warehouses: Dict[int, int] = {}
        for sid in all_shop_ids:
            wh = resolve_warehouse(sid)
            if wh is not None:
                warehouses[sid] = wh

        records = load_records()
        restrictions = db.vc_entries()

        # 2. 查重过滤与店铺候选拆分
        valid_items: List[Dict[str, Any]] = []
        seen = set()
        skip_count = 0
        fail_count = 0

        for it in items:
            target_sids = [sid for sid in (it.target_shops or all_shop_ids) if sid in warehouses]
            if not target_sids:
                print(f"  [跳过] 商品 {it.nm_id} 无可用目标店铺或仓库无法解析")
                fail_count += 1
                continue

            actual_vc = f'{it.prefix}-{it.nm_id}'
            if it.extra.get('vc_source') and it.vendor_code != actual_vc:
                print(f'[跳过] 批量接口无法保留完整 VC {it.vendor_code}，请用 ops create-cards 预览')
                fail_count += 1
                continue
            keys = publish_keys(it) | {actual_vc}
            target_sids = [sid for sid in target_sids
                           if (actual_vc, sid) not in seen
                           and not any(str(sid) in records.get(key, {}) for key in keys)
                           and not db.listing_blocked(it.vendor_code or actual_vc, sid, restrictions)
                           and not db.listing_blocked(actual_vc, sid, restrictions)]
            if check_exists:
                final_targets = []
                for sid in target_sids:
                    if vc_exists_in_shop(it.vendor_code or "", sid, records):
                        continue
                    final_targets.append(sid)
            else:
                final_targets = target_sids

            if not final_targets:
                print(f"  [跳过] 商品 {it.nm_id} ({it.vendor_code}) 目标店铺均已存在或已提交")
                skip_count += 1
                continue
            seen.update((actual_vc, sid) for sid in final_targets)

            p_val = float(it.price or 0.0)
            price_str = str(int(p_val)) if p_val.is_integer() else f"{p_val:.2f}"
            stk = stock_for(it.cn, overrides, default=it.stock)

            # 检查是否使用非前缀式自定义 VC 并提示
            expected_vc = f"{it.prefix}-{it.nm_id}"
            if it.vendor_code and it.vendor_code != expected_vc:
                print(f"  [提示] 新接口上架为平台自动按前缀生成 vendorCode: '{expected_vc}'（传入的完整 VC '{it.vendor_code}' 不会被直接透传；若需原生透传完整 VC 请用 create-cards）")

            valid_items.append({
                "item": it,
                "sku": int(it.nm_id),
                "vc": it.vendor_code or expected_vc,
                "prefix": it.prefix,
                "cn": it.cn,
                "price_str": price_str,
                "packageLength": it.length,
                "packageWidth": it.width,
                "packageHeight": it.height,
                "weightBrut": it.weight,
                "targets": final_targets,
                "stock": stk,
            })

        # 3. dry-run 预览
        if not apply:
            preview_list = []
            for v in valid_items:
                it_copy = copy.copy(v["item"])
                it_copy.target_shops = v["targets"]
                preview_list.append(it_copy)
            print_publish_preview(preview_list, all_shop_ids)
            print(f"\n[dry-run] 共 {len(items)} 个候选，{len(valid_items)} 个待提交上架，跳过 {skip_count} 个。加 --apply 真正执行。")
            result = {"ok": 0, "skip": skip_count, "fail": fail_count, "plans": len(items), "csv": ""}
            if capture and valid_items:
                result['prepared'] = {
                    'items': [{**row, 'item': asdict(row['item'])} for row in valid_items],
                    'warehouses': {sid: warehouses[sid] for row in valid_items for sid in row['targets']},
                    'blocked_count': fail_count,
                }
            return result

    # 4. 执行真正上架
    print(f"\n[开始执行] 新版批量上品接口：{len(valid_items)} 个待上架商品 ...")
    managed_file = False
    csv_file = None
    if writer is None:
        csv_file, writer, csv_path = create_publish_csv(csv_prefix)
        managed_file = True

    def write_log(now_str, it_dict, status_str, reason_str):
        raw_it = it_dict["item"]
        target_str = ",".join(map(str, it_dict["targets"]))
        if row_formatter:
            row = row_formatter({
                "time": now_str,
                "item": raw_it,
                "targets_str": target_str,
                "stock": it_dict["stock"],
                "status": status_str,
                "msg": reason_str,
            })
        else:
            row = [
                now_str, it_dict["sku"], it_dict["vc"], it_dict["cn"], it_dict["price_str"],
                it_dict["packageLength"], it_dict["packageWidth"], it_dict["packageHeight"],
                it_dict["weightBrut"], target_str, it_dict["stock"], status_str, reason_str
            ]
        if writer:
            writer.writerow(row)

    ok_count = 0
    details = []
    t0 = time.time()

    # 按目标店铺组合与库存量进行分组分片
    groups: Dict[Tuple[Tuple[int, ...], int], List[Dict[str, Any]]] = {}
    for vit in valid_items:
        key = (tuple(sorted(vit["targets"])), vit["stock"])
        groups.setdefault(key, []).append(vit)

    total_chunks = sum((len(chunk_list) + BATCH_SIZE - 1) // BATCH_SIZE for chunk_list in groups.values())
    curr_chunk = 0

    for (target_tuple, stock_qty), chunk_items in groups.items():
        shop_configs = [
            {"id": sid, "warehouseId": warehouses[sid], "warehouseQuantity": stock_qty}
            for sid in target_tuple
        ]
        target_str = ",".join(map(str, target_tuple))

        for chunk_idx in range(0, len(chunk_items), BATCH_SIZE):
            curr_chunk += 1
            chunk = chunk_items[chunk_idx:chunk_idx + BATCH_SIZE]
            sku_prices = [
                {
                    "sku": it["sku"],
                    "price": it["price_str"],
                    "packageLength": it["packageLength"],
                    "packageWidth": it["packageWidth"],
                    "packageHeight": it["packageHeight"],
                    "weightBrut": it["weightBrut"],
                    "vendorCodePrefix": it["prefix"],
                }
                for it in chunk
            ]

            print(f"\n[批次 {curr_chunk}/{total_chunks}] 推送 {len(chunk)} 个商品 → 店[{target_str}]（库存={stock_qty}）...")
            now = datetime.now().strftime("%H:%M:%S")
            try:
                resp = submit_once([it['item'] for it in chunk], list(target_tuple),
                                   lambda: bcs.batch_push_products(shop_configs, sku_prices, mode=1, carry_brand=1),
                                   actual_vcs=tuple(f"{it['prefix']}-{it['sku']}" for it in chunk))
                if resp.get("code") == 200:
                    task_id = publish_task_id(resp)
                    task_note = f'taskId={task_id}' if task_id is not None else '平台已受理'
                    print(f"  ✓ 批次提交成功 ({task_note})")
                    for it in chunk:
                        ok_count += 1
                        details.append({'vc': it['vc'], 'targets': list(target_tuple), 'task_id': task_id,
                                        'status': 'accepted',
                                        'mapping_conflict': it['vc'] in resp.get('mapping_conflicts', [])})
                        write_log(now, it, "成功", task_note)
                else:
                    msg = resp.get("msg") or f"code={resp.get('code')}"
                    print(f"  ✗ 批次提交失败: {msg}")
                    for it in chunk:
                        fail_count += 1
                        details.append({'vc': it['vc'], 'targets': list(target_tuple),
                                        'status': 'failed' if type(resp.get('code')) is int else 'unknown'})
                        write_log(now, it, "失败", msg)
            except Exception as e:
                print(f"  ✗ 批次请求异常: {e}")
                for it in chunk:
                    fail_count += 1
                    details.append({'vc': it['vc'], 'targets': list(target_tuple), 'status': 'unknown'})
                    write_log(now, it, "失败", str(e))

            if curr_chunk < total_chunks:
                time.sleep(interval)

    if managed_file and csv_file:
        csv_file.close()
        print(f"\n[汇总] 候选 {len(items)} | 成功 {ok_count} | 跳过 {skip_count} | 失败 {fail_count}（耗时 {time.time() - t0:.1f}s）")
        print(f"明细记录：{csv_path}")

    if ok_count > 0 and managed_file:
        common.print_write_hint()

    return {"ok": ok_count, "skip": skip_count, "fail": fail_count, "plans": len(items), "csv": csv_path or "", "details": details}


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
    """单商品快速上架便捷函数"""
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
    # 1. 提取位置参数商品码
    pos_nms = getattr(args, "nms", None) or []
    if isinstance(pos_nms, str):
        pos_nms = [pos_nms]
    for p in pos_nms:
        for sub in str(p).replace(",", " ").split():
            sub = sub.strip()
            if sub and sub.isdigit():
                raw_nms.append(sub)

    # 2. 提取 --nm 参数商品码
    opt_nm = getattr(args, "nm", "")
    if opt_nm:
        for sub in str(opt_nm).replace(",", " ").split():
            sub = sub.strip()
            if sub and sub.isdigit():
                raw_nms.append(sub)

    # 3. 提取 --file 文件商品码
    file_path = getattr(args, "file", "")
    file_items: List[Dict[str, Any]] = []
    if file_path and os.path.exists(file_path):
        f_items, f_nms = load_publish_file(file_path)
        file_items.extend(f_items)
        raw_nms.extend(f_nms)

    # 去重并保持顺序
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

    # 目标店铺列表解析
    allow_shops = (
        [int(x) for x in args.shops.split(",") if x.strip()]
        if getattr(args, "shops", "")
        else None
    )

    # 尺寸参数解析
    dims_tuple = parse_dims_str(getattr(args, "dims", ""))
    length = dims_tuple[0] if dims_tuple else getattr(args, "length", None)
    width = dims_tuple[1] if dims_tuple else getattr(args, "width", None)
    height = dims_tuple[2] if dims_tuple else getattr(args, "height", None)
    weight = dims_tuple[3] if dims_tuple else getattr(args, "weight", None)

    # 完整 VC 解析支持模版
    file_nms = [int(row.get('nmId') or row.get('sku') or row.get('nm')) for row in file_items]
    codes = publish_codes(getattr(args, 'vc', ''), file_nms + unique_nms)

    shelve_items: List[PublishItem] = []

    # 处理从 file_items 加载的字典结构
    for f_it, f_nm, code in zip(file_items, file_nms, codes[:len(file_items)], strict=True):
        shelve_items.append(PublishItem(
            nm_id=f_nm,
            price=f_it.get("price") or getattr(args, "price", None),
            vendor_code=f_it.get("vendorCode") or f_it.get("vc") or code,
            prefix=f_it.get("prefix") or getattr(args, "prefix", None),
            length=f_it.get("length") or length,
            width=f_it.get("width") or width,
            height=f_it.get("height") or height,
            weight=f_it.get("weight") or weight,
            stock=f_it.get("stock", getattr(args, "stock", DEFAULT_STOCK)),
            target_shops=allow_shops or f_it.get("targetShops"),
            cn=f_it.get("cn") or getattr(args, "cn", ""),
            title=f_it.get("title", ""),
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

    # 处理普通商品码列表
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

    # 逐个补全信息
    resolved_items, resolution_failures = resolve_publish_items(args, shelve_items, bulk=True)

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
    return 1 if resolution_failures or res["fail"] else 0

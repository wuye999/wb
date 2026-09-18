# -*- coding: utf-8 -*-
"""
wb_ops 新版批量上架服务 (shelve_new)

使用 BCS 新版批量上品接口 (POST /products/batch/push) 实现商品自动上架。
可独立作为 CLI 脚本运行，支持单商品/多商品批量、指定完整 VC、前缀码、价格、尺寸重量与目标店铺；
同时作为跨店复制 (replicate) 与他人映射表导入 (import-shelve) 的底层上架引擎。

用法示例：
  python wb.py shelve 248364237 --price 59
  python wb.py shelve 248364237 388854754 --prefix ABCD --dims "10*20*30/0.5" --apply
  python wb.py shelve 248364237 --vc BCS-CUSTOM-12345 --shops 9352,9353 --apply
"""
import argparse
import copy
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wb_ops.domain.models import ShelveItem
from wb_ops.adapters import bcs_client as bcs
from wb_ops.storage.mapping_repo import MappingRepository
from wb_ops.services.catalog_svc import catalog_svc
from wb_ops import config
from wb_ops import common

from wb_ops.services.replicate.shelve_common import (
    DEFAULT_STOCK,
    main_warehouse,
    load_records,
    save_records,
    record_shelve_success,
    vc_exists_in_shop,
    parse_dims_str,
    parse_cn_stock,
    stock_for,
    resolve_item_details,
    create_shelve_csv,
    print_shelve_preview,
    boss_pkg_map,
    load_shelve_file,
)

BATCH_SIZE = 50


def execute_shelve(
    items: List[ShelveItem],
    apply: bool = False,
    interval: float = 1.0,
    sync: bool = False,
    no_verify: bool = False,
    overrides: Optional[Dict[str, int]] = None,
    csv_prefix: str = "新接口上架",
    writer: Optional[Any] = None,
    csv_path: Optional[str] = None,
    row_formatter: Optional[Any] = None,
    check_exists: bool = True,
) -> Dict[str, Any]:
    """
    使用新版批量接口 (POST /products/batch/push) 执行商品上架。

    Args:
        items: 已补全信息的 ShelveItem 实体列表。
        apply: True 为真正调用平台接口执行；False 为 dry-run 预览。
        interval: 批次间请求等待秒数。
        sync: 执行后是否进行快照拉取与映射表合并（默认 False）。
        no_verify: 是否跳过写后验证。
        overrides: 中文名→库存覆盖字典。
        csv_prefix: 输出 CSV 日志文件名前缀。
        writer: 外部传入的 csv.writer（为 None 时内部自动创建）。
        csv_path: 外部传入的 CSV 文件路径。
        row_formatter: 自定义 CSV 行格式化回调。
        check_exists: 是否在线二次查重（调用方已查重时可设为 False）。

    Returns:
        执行统计字典: {"ok": int, "skip": int, "fail": int, "plans": len(items), "csv": str}
    """
    # 1. 获取目标店铺与仓库
    all_shops = bcs.fetch_shop_list()
    all_shop_ids = [s["id"] for s in all_shops if s.get("id")]
    if not all_shop_ids:
        print("[错误] 未获取到可用店铺列表")
        return {"ok": 0, "skip": 0, "fail": len(items), "plans": len(items), "csv": ""}

    warehouses: Dict[int, int] = {}
    for sid in all_shop_ids:
        wh = main_warehouse(sid)
        if wh is not None:
            warehouses[sid] = wh

    records = load_records()

    # 2. 查重过滤与店铺候选拆分
    valid_items: List[Dict[str, Any]] = []
    skip_count = 0
    fail_count = 0

    for it in items:
        target_sids = [sid for sid in (it.target_shops or all_shop_ids) if sid in warehouses]
        if not target_sids:
            print(f"  [跳过] 商品 {it.nm_id} 无可用目标店铺或仓库无法解析")
            skip_count += 1
            continue

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

        p_val = float(it.price or 0.0)
        price_str = str(int(p_val)) if p_val.is_integer() else f"{p_val:.2f}"
        stk = stock_for(it.cn, overrides, default=it.stock)

        # 检查是否使用非前缀式自定义 VC 并提示
        expected_vc = f"{it.prefix}-{it.nm_id}"
        if it.vendor_code and it.vendor_code != expected_vc:
            print(f"  [提示] 新接口上架为平台自动按前缀生成 vendorCode: '{expected_vc}'（传入的完整 VC '{it.vendor_code}' 不会被直接透传；若需原生透传完整 VC 请用 shelve-old）")

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
        print_shelve_preview(preview_list, all_shop_ids)
        print(f"\n[dry-run] 共 {len(items)} 个候选，{len(valid_items)} 个待提交上架，跳过 {skip_count} 个。加 --apply 真正执行。")
        return {"ok": 0, "skip": skip_count, "fail": fail_count, "plans": len(items), "csv": ""}

    # 4. 执行真正上架
    print(f"\n[开始执行] 新版批量上品接口：{len(valid_items)} 个待上架商品 ...")
    managed_file = False
    csv_file = None
    if writer is None:
        csv_file, writer, csv_path = create_shelve_csv(csv_prefix)
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
                resp = bcs.batch_push_products(shop_configs, sku_prices, mode=1, carry_brand=1)
                if resp.get("code") == 200:
                    task_id = (resp.get("data") or {}).get("taskId") or "已提交"
                    print(f"  ✓ 批次提交成功 (taskId={task_id})")
                    for it in chunk:
                        ok_count += 1
                        raw_item = it["item"]
                        write_log(now, it, "成功", f"taskId={task_id}")
                        record_shelve_success(records, raw_item, list(target_tuple))
                    save_records(records)
                else:
                    msg = resp.get("msg") or f"code={resp.get('code')}"
                    print(f"  ✗ 批次提交失败: {msg}")
                    for it in chunk:
                        fail_count += 1
                        write_log(now, it, "失败", msg)
            except Exception as e:
                print(f"  ✗ 批次请求异常: {e}")
                for it in chunk:
                    fail_count += 1
                    write_log(now, it, "失败", str(e))

            if curr_chunk < total_chunks:
                time.sleep(interval)

    if managed_file and csv_file:
        csv_file.close()
        print(f"\n[汇总] 候选 {len(items)} | 成功 {ok_count} | 跳过 {skip_count} | 失败 {fail_count}（耗时 {time.time() - t0:.1f}s）")
        print(f"明细记录：{csv_path}")

    # 写后提示与非必选写后同步
    if ok_count > 0 and sync and not no_verify:
        print("\n[写后验证] 触发全店同步 + 合并映射表...")
        try:
            catalog_svc.fetch_all_shops_products()
            catalog_svc.post_write_merge(fetch=False)
        except Exception as e:
            print(f"[验证异常] {e}")
    elif ok_count > 0 and managed_file:
        common.print_write_hint()

    return {"ok": ok_count, "skip": skip_count, "fail": fail_count, "plans": len(items), "csv": csv_path or ""}



def shelve_product(
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
    item = ShelveItem(
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
    resolved, err = resolve_item_details(item)
    if err or not resolved:
        print(f"[参数错误] {err}")
        return {"ok": 0, "skip": 0, "fail": 1, "plans": 1, "csv": ""}
    return execute_shelve([resolved], apply=apply)


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器"""
    ap = argparse.ArgumentParser(
        prog="wb.py shelve",
        description="Wildberries 新版批量商品上架（输入 WB 商品码自动解析并推送到我的店铺）",
    )
    ap.add_argument("nms", nargs="*", help="WB 商品码列表（支持空格或逗号分隔，如 248364237 388854754）")
    ap.add_argument("--nm", default="", help="WB 商品码（逗号分隔多个，兼容命令行参数传参）")
    ap.add_argument("--vc", default="", help="完整 vendorCode（指定单个商品完整 VC；多商品时可用模版如 BCS-TAG-{nm} 或逗号分隔）")
    ap.add_argument("--prefix", default="", help="4位前缀码（如 ABCD 或 BCS-ABCD）")
    ap.add_argument("--price", type=float, default=None, help="指定售价（CNY），不填时自动从本地价格映射表或商品价格表推导")
    ap.add_argument("--dims", default="", help="自定义尺寸重量 '长*宽*高/毛重'（例: 10*20*30/0.5）")
    ap.add_argument("--length", type=int, default=None, help="包装长 (cm)")
    ap.add_argument("--width", type=int, default=None, help="包装宽 (cm)")
    ap.add_argument("--height", type=int, default=None, help="包装高 (cm)")
    ap.add_argument("--weight", type=float, default=None, help="包装毛重 (kg)")
    ap.add_argument("--shops", default="", help="目标店铺 ID（逗号分隔，如 9352,9353；默认上架到全部活跃店铺）")
    ap.add_argument("--stock", type=int, default=DEFAULT_STOCK, help=f"上架库存量（默认 {DEFAULT_STOCK}）")
    ap.add_argument("--cn", default="", help="商品中文名（辅助匹配价格与包装规格）")
    ap.add_argument("--file", default="", help="批量上架文件路径（支持 .txt 每行一个商品码，或 .json 商品列表）")
    ap.add_argument("--cn-stock", default="", help="按中文名指定库存（'中文名:库存,...'）")
    ap.add_argument("--interval", type=float, default=1.0, help="批次请求间隔秒（默认 1.0）")
    ap.add_argument("--apply", action="store_true", help="真正调用平台接口执行（默认仅 dry-run 预览）")
    ap.add_argument("--sync", action="store_true", help="执行完成后触发全量同步并合并映射表")
    ap.add_argument("--no-verify", action="store_true", help="跳过写后验证")
    return ap


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
        f_items, f_nms = load_shelve_file(file_path)
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
        print("[错误] 未指定任何 WB 商品码，请传入商品码或 --file 文件。")
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
    raw_vc = getattr(args, "vc", "").strip()
    vc_list = [v.strip() for v in raw_vc.split(",") if v.strip()] if "," in raw_vc else []

    # 加载本地状态缓存一次
    mapping_state, _ = MappingRepository.load_mapping_state()
    pmap = MappingRepository.load_prefix_map()
    boss_pkg = boss_pkg_map()
    card_cache: Dict[int, Any] = {}

    shelve_items: List[ShelveItem] = []

    # 处理从 file_items 加载的字典结构
    for f_it in file_items:
        f_nm = int(f_it.get("nmId") or f_it.get("sku") or f_it.get("nm"))
        shelve_items.append(ShelveItem(
            nm_id=f_nm,
            price=f_it.get("price") or getattr(args, "price", None),
            vendor_code=f_it.get("vendorCode") or f_it.get("vc"),
            prefix=f_it.get("prefix") or getattr(args, "prefix", None),
            length=f_it.get("length") or length,
            width=f_it.get("width") or width,
            height=f_it.get("height") or height,
            weight=f_it.get("weight") or weight,
            stock=f_it.get("stock") or getattr(args, "stock", DEFAULT_STOCK),
            target_shops=allow_shops or f_it.get("targetShops"),
            cn=f_it.get("cn") or getattr(args, "cn", ""),
            title=f_it.get("title", ""),
            extra={
                **(f_it.get("extra") or {}),
                "orig_vc": f_it.get("orig_vc"),
                "orig_nm": f_it.get("orig_nm"),
                "raw_cur_nm": f_it.get("raw_cur_nm"),
            }
        ))

    # 处理普通商品码列表
    for idx, nm in enumerate(unique_nms):
        item_vc = None
        if raw_vc:
            if "{nm}" in raw_vc:
                item_vc = raw_vc.format(nm=nm)
            elif vc_list and idx < len(vc_list):
                item_vc = vc_list[idx]
            elif len(unique_nms) == 1:
                item_vc = raw_vc

        shelve_items.append(ShelveItem(
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

    # 逐个补全信息
    resolved_items: List[ShelveItem] = []
    for it in shelve_items:
        resolved, err = resolve_item_details(it, mapping_state, pmap, boss_pkg, card_cache)
        if err or not resolved:
            print(f"[跳过] 商品 {it.nm_id}: {err}")
            continue
        resolved_items.append(resolved)

    if not resolved_items:
        print("[错误] 没有可执行的有效上架任务（全部商品信息校验失败）。")
        return 1

    overrides = parse_cn_stock(getattr(args, "cn_stock", "") or "")
    res = execute_shelve(
        resolved_items,
        apply=getattr(args, "apply", False),
        interval=getattr(args, "interval", 1.0),
        sync=getattr(args, "sync", False),
        no_verify=getattr(args, "no_verify", False),
        overrides=overrides,
    )
    return 0 if (res["ok"] > 0 or not getattr(args, "apply", False)) else 1


def main():
    common.ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()

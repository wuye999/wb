# -*- coding: utf-8 -*-
"""
wb_ops 旧版单品多店上架服务 (shelve_old)

使用 BCS 旧版上品建卡接口 (POST /system/wbCollection/wb/new) 实现商品上架。
来源自 2026-09-09 备份接口，支持自定义完整 vendorCode（不受新接口前缀格式限制）、
自动提取 card.json 与 WB detail、多目标店铺单次请求推送。
可独立作为 CLI 脚本运行，亦作为 replicate 与 import-shelve 的无缝可替换依赖。

用法示例：
  python wb.py shelve-old 248364237 --price 59
  python wb.py shelve-old 248364237 --vc BCS-CUSTOM-SPECIAL-12345 --apply
  python wb.py shelve-old 248364237 388854754 --prefix ABCD --dims "10*20*30/0.5" --apply
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
    fetch_wb_detail_bcs,
    build_synthetic_detail,
    fetch_card_json,
    generate_image_urls,
    boss_pkg_map,
    load_shelve_file,
)


def build_legacy_payload(
    item: ShelveItem,
    target_sids: List[int],
    warehouses: Dict[int, int],
    card_info: Dict[str, Any],
    detail: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, int]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    构造 /system/wbCollection/wb/new 请求体。
    支持任意合法完整 vendorCode。返回 (payload, err)。
    """
    p_val = float(item.price or 0.0)
    price_str = f"{p_val:.2f}"

    # 1. wbDetail 详情组装与 sizes 重写
    wb_detail = copy.deepcopy(detail) if detail else None
    ci_data = (card_info or {}).get("data") or {}
    subject_id_candidate = (
        item.extra.get("subjectId")
        or item.extra.get("subject_id")
        or ci_data.get("subject_id")
        or (card_info or {}).get("subject_id")
        or (card_info or {}).get("subjectId")
    )
    if not wb_detail:
        wb_detail = build_synthetic_detail(
            card_info=card_info,
            nm_id=item.nm_id,
            title=item.title,
            weight=float(item.weight or 0.0),
            vendor_code=item.vendor_code or "",
            price_str=price_str,
            subject_id=subject_id_candidate,
        )

    sizes = wb_detail.get("sizes") or []
    if not sizes:
        sizes = [{"origName": 0, "name": "", "price": price_str, "vendorCode": item.vendor_code}]
    else:
        # 只保留第一条规格，避免误上兄弟规格
        s0 = sizes[0]
        orig = s0.get("origName")
        if orig is None:
            orig = s0.get("techSize") if s0.get("techSize") else 0
        try:
            orig = int(float(orig))
        except (TypeError, ValueError):
            orig = 0
        sizes = [{
            "origName": orig,
            "name": s0.get("name") if s0.get("name") is not None else (s0.get("wbSize") or ""),
            "price": price_str,
            "vendorCode": item.vendor_code,
        }]
    wb_detail["sizes"] = sizes

    # 2. 类目与层级 ID 健壮解析
    subject_id = wb_detail.get("subjectId") or subject_id_candidate
    if not subject_id:
        return None, "类目 ID 缺失（WB 详情与 card.json 均无 subjectId）"

    parent_id = (
        wb_detail.get("subjectParentId")
        or ci_data.get("subject_root_id")
        or (card_info or {}).get("subject_root_id")
        or (card_info or {}).get("subjectParentId")
        or 0
    )
    parent_name = (card_info or {}).get("subj_root_name") or ""
    subject_name = (card_info or {}).get("subj_name") or wb_detail.get("subjectName") or ""

    # 3. 图片资源健壮补齐（绝不允许提交空字符串）
    images = item.images
    if not images:
        photo_count = ((card_info or {}).get("media") or {}).get("photo_count") or 1
        images = ";".join(generate_image_urls(item.nm_id, photo_count))
    main_image = item.main_image or (images.split(";")[0] if images else "")

    title = item.title or (card_info or {}).get("imt_name") or wb_detail.get("name") or ""

    stk = stock_for(item.cn, overrides, default=item.stock)
    shop_arr = []
    for sid in target_sids:
        wh_id = warehouses.get(sid)
        if wh_id is None:
            return None, f"店铺 {sid} 缺少对应发货仓库配置"
        shop_arr.append({"id": sid, "warehouseId": wh_id, "warehouseQuantity": stk})

    payload = {
        "shop": json.dumps(shop_arr, ensure_ascii=False, separators=(",", ":")),
        "offerDIY": "",
        "brandStatus": False,
        "oModel": True,
        "status": "1",
        "collectionType": "0",
        "shopDatas": [{
            # ★ nmId 提交前必须置空 null，避免被判定为更新已有卡
            "nmId": None,
            "wbDetail": json.dumps(wb_detail, ensure_ascii=False, separators=(",", ":")),
            "name": title,
            "subjectId": subject_id,
            "parentId": parent_id,
            "wbCardInfo": json.dumps(card_info, ensure_ascii=False, separators=(",", ":")),
            "images": images,
            "video": "",
            "mainImage": main_image,
            "packagLength": math.ceil(float(item.length or 0)),
            "packagWidth": math.ceil(float(item.width or 0)),
            "packagHeight": math.ceil(float(item.height or 0)),
            "weightBrutto": str(item.weight or 0),
            "vendorCode": item.vendor_code,
            "brand": "",
            "hsCode": None,
            "subjectName": subject_name,
            "parentName": parent_name,
            "sourceSku": str(item.nm_id),
        }],
        "mode": 1,
        "mergeCards": 1,
        "carryBrand": 1,
        "customBrand": None,
        "titleSuffix": None,
        "aiRewrite": False,
        "aiRetouch": False,
        "aiRetouchTemplateId": None,
        "imgUploadMode": 0,
    }
    return payload, None


def execute_shelve(
    items: List[ShelveItem],
    apply: bool = False,
    interval: float = 1.0,
    sync: bool = False,
    no_verify: bool = False,
    overrides: Optional[Dict[str, int]] = None,
    csv_prefix: str = "旧接口上架",
    writer: Optional[Any] = None,
    csv_path: Optional[str] = None,
    row_formatter: Optional[Any] = None,
    check_exists: bool = True,
) -> Dict[str, Any]:
    """
    使用旧版接口 (POST /system/wbCollection/wb/new) 执行商品上架。

    Args:
        items: 已补全信息的 ShelveItem 实体列表。
        apply: True 为真正调用平台接口执行；False 为 dry-run 预览。
        interval: 商品间请求等待秒数。
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

    # 查重与过滤
    valid_items: List[Tuple[ShelveItem, List[int]]] = []
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

        valid_items.append((it, final_targets))

    # dry-run 预览
    if not apply:
        preview_list = []
        for it, final_targets in valid_items:
            it_copy = copy.copy(it)
            it_copy.target_shops = final_targets
            preview_list.append(it_copy)
        print_shelve_preview(preview_list, all_shop_ids)
        print(f"\n[dry-run] 共 {len(items)} 个候选，{len(valid_items)} 个待提交上架，跳过 {skip_count} 个。加 --apply 真正执行。")
        return {"ok": 0, "skip": skip_count, "fail": fail_count, "plans": len(items), "csv": ""}

    # 真正执行
    print(f"\n[开始执行] 旧版上品建卡接口：{len(valid_items)} 个待上架商品 ...")
    managed_file = False
    csv_file = None
    if writer is None:
        csv_file, writer, csv_path = create_shelve_csv(csv_prefix)
        managed_file = True

    def write_log(now_str, it, targets_str, stk_val, status_str, reason_str):
        if row_formatter:
            row = row_formatter({
                "time": now_str,
                "item": it,
                "targets_str": targets_str,
                "stock": stk_val,
                "status": status_str,
                "msg": reason_str,
            })
        else:
            p_val = float(it.price or 0.0)
            p_str = str(int(p_val)) if p_val.is_integer() else f"{p_val:.2f}"
            row = [
                now_str, it.nm_id, it.vendor_code, it.cn, p_str,
                it.length, it.width, it.height, it.weight, targets_str, stk_val,
                status_str, reason_str,
            ]
        if writer:
            writer.writerow(row)

    ok_count = 0
    t0 = time.time()

    for idx, (it, target_sids) in enumerate(valid_items, 1):
        tag = f"[{idx}/{len(valid_items)}]"
        now = datetime.now().strftime("%H:%M:%S")
        target_str = ",".join(map(str, target_sids))
        stk = stock_for(it.cn, overrides, default=it.stock)

        # 1. 抓取/补全 card_info 与 detail
        card_info = it.extra.get("card_info")
        if not card_info:
            card_info = fetch_card_json(it.nm_id)
            time.sleep(0.2)

        if not card_info:
            print(f"  {tag} [失败] 商品 {it.nm_id} card.json 获取失败")
            fail_count += 1
            write_log(now, it, target_str, stk, "失败", "card.json获取失败")
            continue

        detail = it.extra.get("wb_detail")
        if not detail:
            detail = fetch_wb_detail_bcs(it.nm_id)
            time.sleep(0.3)

        # 2. 构造载荷
        payload, err = build_legacy_payload(it, target_sids, warehouses, card_info, detail, overrides)
        if err or not payload:
            print(f"  {tag} [失败] 商品 {it.nm_id} 载荷构造失败: {err}")
            fail_count += 1
            write_log(now, it, target_str, stk, "失败", err or "载荷构造失败")
            continue

        # 3. 提交请求
        url = f"{bcs.client.base_url}/system/wbCollection/wb/new"
        try:
            resp = bcs.client.post(url, payload)
            if resp.get("code") == 200:
                ok_count += 1
                print(f"  {tag} ✓ [成功] {it.vendor_code} (nm={it.nm_id}) → 店[{target_str}]（{it.price}元）")
                write_log(now, it, target_str, stk, "成功", "")
                record_shelve_success(records, it, target_sids)
                save_records(records)
            else:
                msg = resp.get("msg") or f"code={resp.get('code')}"
                print(f"  {tag} ✗ [失败] {it.vendor_code} 店[{target_str}]: {msg}")
                fail_count += 1
                write_log(now, it, target_str, stk, "失败", msg)
        except Exception as e:
            print(f"  {tag} ✗ [异常] {it.vendor_code}: {e}")
            fail_count += 1
            write_log(now, it, target_str, stk, "失败", str(e))

        if idx < len(valid_items):
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
    """单商品快速上架便捷函数（旧接口）"""
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
        prog="wb.py shelve-old",
        description="Wildberries 旧版单品多店上架（支持自定义完整 VC、俄文详情与主图直上）",
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
    ap.add_argument("--interval", type=float, default=1.0, help="单品请求间隔秒（默认 1.0）")
    ap.add_argument("--apply", action="store_true", help="真正调用平台接口执行（默认仅 dry-run 预览）")
    ap.add_argument("--sync", action="store_true", help="执行完成后触发全量同步并合并映射表")
    ap.add_argument("--no-verify", action="store_true", help="跳过写后验证")
    return ap


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
        f_items, f_nms = load_shelve_file(file_path)
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
    boss_pkg = boss_pkg_map()
    card_cache: Dict[int, Any] = {}

    shelve_items: List[ShelveItem] = []

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

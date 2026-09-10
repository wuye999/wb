# -*- coding: utf-8 -*-
"""
wb_ops 他人映射表导入上架（import-shelve）

把他人（同项目格式）映射表中有、我方全无的商品，上架到我的店铺：
- 差集与防重判断：使用供应商代码里的「原始WB商品码」（从 vendorCode 截取数字）进行去重与我方已有判断；
  （背景：WB 平台在上架后会为每个卖家分配全新的唯一 WB 商品码，跨卖家/跨店铺商品码互不相同；
   只有供应商代码里的原始 WB 商品码才是同源商品在不同店铺间的一致标识，用于可靠防重）。
- 上架 API 抓取目标：新版批量上品接口（POST /products/batch/push）提交的 sku 必须为他人映射表的「WB商品码」列
  （他人店铺当前在售商品码，可实时拉取详情与主图）；必须非空且纯数字，无此码坚决跳过。
- 前缀码决策：中文名匹配我方商品价格表前缀码优先 → 提取他人 vendorCode 4位前缀 → 随机 4 位大写字母（均自动拼接 BCS- 前缀）。
- 价格：他人表「双倍售价」列，店铺价 = floor(双倍售价)（我方标准定价规则）。
- 包装尺寸：他人表（长/宽/高/毛重）→ 我方商品价格表同中文名兜底 → card.json 兜底。

用法：wb.py import-shelve <他人映射表.xlsx> [--cn|--shops|--limit] [--apply] [--no-verify] [--interval S]
"""
import csv
import json
import math
import os
import time
from datetime import datetime

import openpyxl

from . import bcs
from . import common
from . import config
from . import products
from . import replicate

# 本地提交记录（与 replicate 共用文件；键 = 原始WB码 k，跨账号防重稳定）
RECORDS_JSON = replicate.RECORDS_JSON
BATCH_SIZE = replicate.BATCH_SIZE

# 他人表「映射总表」Sheet 用到的列名（按表头定位，缺列容错）
COL_CN = "产品中文名"
COL_VC = "vendorCode"
COL_DP = "双倍售价"
COL_TITLE = "俄文标题"
COL_IMG = "主图链接"
COL_L = "尺寸长(cm)"
COL_W = "尺寸宽(cm)"
COL_H = "尺寸高(cm)"
COL_WT = "毛重(kg)"
COL_NM = "WB商品码"  # 他人店铺当前可靠 nmId（抓取上品数据必须项，无此列跳过）


# ---------------- 兼容工具函数 ----------------
boss_pkg_map = replicate.boss_pkg_map


def nm_of(vc):
    """从 vendorCode 提取原始 WB 商品码（用于跨账号跨店铺查重与差集比对）"""
    return common.extract_wb_nm(vc)


OZON_BADGE = "ozon-card"


def vc_tail_badge(vc):
    """提取他人 vc 除去前缀与末段 nm 后的中间修饰段（如 ozon-card 或 WRLINWI/ 等）"""
    import re
    s = str(vc or "").strip()
    if "/" in s:
        m = re.match(r"^BCS-[A-Z]{4}-([^/]+/)\d+$", s)
        if m:
            return m.group(1)
    elif f"-{OZON_BADGE}-" in s:
        return f"{OZON_BADGE}-"
    return ""


# ---------------- 他人表解析 ----------------
def load_foreign(xlsx_path):
    """解析他人映射表「映射总表」Sheet → (items, bad_vcs, no_nm_count)
    items: [{cn, vc, k, nm, dp, title_ru, img, L, W, H, weight}]，同原始 WB 码 k 去重（优先双倍售价非空行）
      k: = common.extract_wb_nm(vc)，供应商代码里的原始 WB 商品码（跨店铺防重复上架判断的核心键）
      nm: = 他人表「WB商品码」列（他人店铺当前可靠 nmId，新上架 API 提交抓取的 sku，必须非空且纯数字）
    bad_vcs: 供应商代码无法提取原始 WB 码的异常行
    no_nm_count: 缺少有效 WB商品码（无法用于新接口抓取上品）被跳过的行数
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    if "映射总表" not in wb.sheetnames:
        wb.close()
        raise RuntimeError(f"{xlsx_path} 缺少「映射总表」Sheet（需同项目生成的映射表格式）")
    ws = wb["映射总表"]
    headers = [str(c.value or "") for c in ws[1]]

    def idx(name):
        return headers.index(name) if name in headers else None

    i_cn, i_vc, i_dp = idx(COL_CN), idx(COL_VC), idx(COL_DP)
    i_title, i_img = idx(COL_TITLE), idx(COL_IMG)
    i_l, i_w, i_h, i_wt = idx(COL_L), idx(COL_W), idx(COL_H), idx(COL_WT)
    i_nm = idx(COL_NM)

    if i_vc is None or i_cn is None:
        wb.close()
        raise RuntimeError(f"表头缺少 {COL_VC}/{COL_CN} 列，无法解析")
    if i_nm is None:
        wb.close()
        print(f"[错误] 他人表缺少「{COL_NM}」列，无法读取抓取上品所需的可靠 WB商品码！按规则坚决跳过，无法导入。")
        return [], [], 0

    by_k, bad_vcs = {}, []
    no_nm_count = 0
    for r in ws.iter_rows(min_row=2, values_only=True):
        vc = str(r[i_vc] or "").strip()
        cn = str(r[i_cn] or "").strip() if i_cn is not None else ""
        if not vc and not cn:
            continue

        # 1. 提取供应商代码里的原始 WB 商品码（查重防重核心）
        k = nm_of(vc)
        if not k:
            bad_vcs.append(vc)
            continue

        # 2. 提取他人店铺在售 WB商品码（API 抓取上品目标 sku，做严格非空与纯数字校验）
        raw_nm = r[i_nm] if i_nm is not None else None
        nm = str(raw_nm).strip() if raw_nm not in (None, "") else ""
        if not nm or not nm.isdigit():
            no_nm_count += 1
            continue

        item = {
            "cn": cn,
            "vc": vc,
            "k": k,
            "nm": nm,
            "dp": r[i_dp] if i_dp is not None else None,
            "title_ru": str(r[i_title] or "").strip() if i_title is not None else "",
            "img": str(r[i_img] or "").strip() if i_img is not None else "",
            "L": r[i_l] if i_l is not None else None,
            "W": r[i_w] if i_w is not None else None,
            "H": r[i_h] if i_h is not None else None,
            "weight": r[i_wt] if i_wt is not None else None,
        }
        old = by_k.get(k)
        if old is None or (old["dp"] is None and item["dp"] is not None):
            by_k[k] = item

    wb.close()
    return list(by_k.values()), bad_vcs, no_nm_count


# ---------------- 包装数据提取 ----------------
def resolve_import_package(item, card_cache=None):
    """
    获取导入上架的包装尺寸与重量：
    1. 优先读取他人表 (item) 的 L, W, H, weight
    2. 若缺失或 <= 0，查商品价格表 (boss_pkg_map) 兜底
    3. 若仍缺失，查 card.json 兜底
    返回: ((L, W, H, weight), None) 或 (None, err_msg)
    """
    length = item.get("L")
    width = item.get("W")
    height = item.get("H")
    weight = item.get("weight")
    cn = item.get("cn") or ""
    nm = item.get("nm")

    def _valid(val):
        try:
            return float(val) > 0
        except (TypeError, ValueError):
            return False

    # 2. 商品价格表兜底
    if not (_valid(length) and _valid(width) and _valid(height) and _valid(weight)):
        bp = replicate.boss_pkg_map().get(cn)
        if bp:
            bl, bw, bh, bwgt = bp
            if not _valid(length):
                length = bl
            if not _valid(width):
                width = bw
            if not _valid(height):
                height = bh
            if not _valid(weight):
                weight = bwgt

    # 3. card.json 兜底
    if not (_valid(length) and _valid(width) and _valid(height) and _valid(weight)):
        card_info = None
        if card_cache is not None and nm in card_cache:
            card_info = card_cache[nm]
        else:
            card_info = replicate.fetch_card_json(nm)
            if card_cache is not None:
                card_cache[nm] = card_info
        if card_info:
            pkg = replicate.parse_package_info(card_info)
            if not _valid(length):
                length = pkg.get("length")
            if not _valid(width):
                width = pkg.get("width")
            if not _valid(height):
                height = pkg.get("height")
            if not _valid(weight):
                weight = pkg.get("weight")

    if not (_valid(length) and _valid(width) and _valid(height) and _valid(weight)):
        return None, f"包装数据缺失（L={length} W={width} H={height} 重={weight}）"

    return (
        math.ceil(float(length)),
        math.ceil(float(width)),
        math.ceil(float(height)),
        round(float(weight), 3)
    ), None


# ---------------- 差集与候选计划 ----------------
def diff_foreign(foreign):
    """他人表 - 我方快照与映射表 → 候选清单 [(item, prefix, prefix_from)]。
    核心逻辑：使用供应商代码里的「原始WB商品码」进行去重与差集比对，防止重复上架。
    """
    from . import mapping
    shops_data, _ = products.load_all_shops()
    _, _, all_shop_ids = replicate.build_coverage(shops_data)

    # 收集我方已有商品的原始 WB 码集合（从 vendorCode 提取末段数字）
    my_nms = set()
    # 1. 各店快照中的在架 vendorCode
    for sid, rows in shops_data.items():
        for r in rows:
            if r.get("trashedAt"):
                continue
            orig = nm_of(r.get("vendorCode"))
            if orig:
                my_nms.add(orig)

    # 2. 我方价格映射表中的全部 vendorCode
    try:
        mapping_state, _ = mapping.load_mapping_state()
        for vc in mapping_state.keys():
            orig = nm_of(vc)
            if orig:
                my_nms.add(orig)
    except Exception:
        pass

    # 我方商品价格表前缀码映射：{中文名: 前缀}
    pmap = mapping.load_prefix_map()
    cn2prefix = {}
    for prefix, info in pmap.items():
        cn2prefix.setdefault(info.get("cn") or "", prefix)

    records = replicate._load_records()
    plans, have_cnt, rec_skip = [], 0, 0

    for item in sorted(foreign, key=lambda x: x["k"]):
        k = item["k"]
        if k in my_nms:
            have_cnt += 1
            continue
        if k in records:
            rec_skip += 1
            continue

        prefix, prefix_from = replicate.resolve_vendor_prefix(item["cn"], item["vc"], cn2prefix)
        plans.append((item, prefix, prefix_from))

    return plans, all_shop_ids, have_cnt, rec_skip, cn2prefix


# ---------------- 主流程 ----------------
def run(args):
    overrides = replicate.parse_cn_stock(getattr(args, "cn_stock", "") or "")
    replicate.ensure_snapshots(args)
    foreign, bad_vcs, no_nm_count = load_foreign(args.xlsx)

    if not foreign:
        print("[结束] 他人表无有效商品（或因缺少/空WB商品码已被跳过）")
        return 0

    plans, all_shop_ids, have_cnt, rec_skip, cn2prefix = diff_foreign(foreign)

    # --cn 过滤 / --shops 限定目标店 / --limit
    if getattr(args, "cn", ""):
        kws = [k.strip() for k in args.cn.split(",") if k.strip()]
        plans = [p for p in plans if any(kw in (p[0]["cn"] or "") for kw in kws)]
    allow_shops = ([int(x) for x in args.shops.split(",") if x.strip()]
                   if getattr(args, "shops", "") else None)
    target_shops = allow_shops or all_shop_ids
    if getattr(args, "limit", 0):
        plans = plans[:args.limit]

    print(f"他人表：{len(foreign)} 个唯一商品（跳过无WB商品码 {no_nm_count} 行，格式异常 {len(bad_vcs)} 行）")
    if bad_vcs:
        print("  [格式异常] " + ", ".join(bad_vcs[:10]) + ("..." if len(bad_vcs) > 10 else ""))
    print(f"我方已有 {have_cnt} | 本地记录已提交 {rec_skip} | 待上架候选 {len(plans)} 个")
    print(f"目标店铺: {target_shops}")

    # dry-run 清单
    print("\n" + "=" * 122)
    print(f"{'原始WB码(查重)':<14}{'抓取WB码':<14}{'中文名':<16}{'双倍售价→店铺价':<18}{'库存':<6}{'提交前缀(来源)':<22}{'他人vc':<26}目标店")
    print("-" * 122)
    for item, prefix, prefix_from in plans:
        dp = item["dp"]
        shop_price = math.floor(float(dp)) if dp is not None and float(dp) > 0 else "?"
        stk = replicate.stock_for(item["cn"], overrides)
        pref_desc = f"{prefix}({prefix_from})"
        print(f"{item['k']:<14}{item['nm']:<14}{(item['cn'] or '-'):<16}{f'{dp}→{shop_price}' if dp else '?':<18}"
              f"{stk:<6}{pref_desc:<22}{item['vc']:<26}{','.join(map(str, target_shops))}")
    print("=" * 122)

    if not plans:
        print("无可执行任务")
        return 0
    if not args.apply:
        print(f"\n[dry-run] 共 {len(plans)} 个商品待上架。加 --apply 执行。")
        return 0

    # ---- 执行 ----
    print(f"\n开始执行：{len(plans)} 个商品 ...")
    shops_data, _ = products.load_all_shops()
    warehouses = {}
    for sid in target_shops:
        wh = replicate.main_warehouse(sid, shops_data)
        if wh is None:
            print(f"  [警告] 店 {sid} 无法解析主仓库，该店目标将跳过")
        else:
            warehouses[sid] = wh
    print(f"各店主仓库: {warehouses}")
    if not warehouses:
        print("[错误] 无可用目标仓库")
        return 1

    records = replicate._load_records()
    card_cache = {}

    csv_path = os.path.join(config.LOG_DIR, f"导入上架_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(config.LOG_DIR, exist_ok=True)
    csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(csv_file)
    writer.writerow(["时间", "原始WB码", "抓取WB码", "中文名", "他人vc", "提交前缀", "前缀来源", "目标店", "店铺价", "库存", "结果", "原因"])

    ok = skip = fail = 0
    t0 = time.time()

    # 1) 逐个商品进行前置校验与包装尺寸提取
    valid_items = []
    for i, (item, prefix, prefix_from) in enumerate(plans, 1):
        tag = f"[{i}/{len(plans)}]"
        k, nm, cn = item["k"], item["nm"], item["cn"] or "-"
        dp = item["dp"]
        now = datetime.now().strftime("%H:%M:%S")

        # 0) 严格 WB商品码校验（API 抓取上品目标）
        if not (nm and nm.isdigit()):
            print(f"  {tag} [跳过] 无有效抓取WB商品码")
            writer.writerow([now, k, nm, cn, item["vc"], prefix, prefix_from,
                             ",".join(map(str, target_shops)), "-", "-", "跳过", "无有效抓取WB商品码"])
            skip += 1
            continue

        # 价格校验
        if dp is None or float(dp) <= 0:
            print(f"  {tag} [失败] k={k} 双倍售价缺失或无效")
            writer.writerow([now, k, nm, cn, item["vc"], prefix, prefix_from,
                             ",".join(map(str, target_shops)), "-", "-", "失败", "双倍售价缺失或无效"])
            fail += 1
            continue

        shop_price = math.floor(float(dp))
        price_str = str(shop_price)
        stk = replicate.stock_for(item["cn"], overrides)

        # 查重：按原始 WB 码 k 在本地记录过滤已提交店铺
        valid_targets = []
        for sid in target_shops:
            if sid not in warehouses:
                continue
            if str(sid) in (records.get(k) or {}):
                print(f"  {tag} [跳过] 店{sid} 本地记录已提交 原始WB码={k}")
                continue
            valid_targets.append(sid)

        if not valid_targets:
            print(f"  {tag} [跳过] 原始WB码={k} 目标店均已存在或已提交")
            writer.writerow([now, k, nm, cn, item["vc"], prefix, prefix_from,
                             ",".join(map(str, target_shops)), price_str, stk, "跳过", "目标店均已存在或已提交"])
            skip += 1
            continue

        # 包装尺寸提取：他人表 → 商品价格表 → card.json
        dims, err = resolve_import_package(item, card_cache)
        if err:
            print(f"  {tag} [失败] k={k}: {err}")
            writer.writerow([now, k, nm, cn, item["vc"], prefix, prefix_from,
                             ",".join(map(str, valid_targets)), price_str, stk, "失败", err])
            fail += 1
            continue

        valid_items.append({
            "item": item,
            "k": k,
            "nm": nm,
            "cn": cn,
            "sku": int(nm),  # 新接口上品提交的 sku：他人表有效抓取 WB 商品码
            "price_str": price_str,
            "packageLength": dims[0],
            "packageWidth": dims[1],
            "packageHeight": dims[2],
            "weightBrut": dims[3],
            "vendorCodePrefix": prefix,
            "prefix_from": prefix_from,
            "valid_targets": valid_targets,
            "stock": stk,
        })

    # 2) 按目标店铺与库存分组，每组按 BATCH_SIZE (50) 分片推送
    groups = {}
    for it in valid_items:
        key = (tuple(sorted(it["valid_targets"])), it["stock"])
        groups.setdefault(key, []).append(it)

    total_chunks = sum((len(items) + BATCH_SIZE - 1) // BATCH_SIZE for items in groups.values())
    curr_chunk = 0

    for (target_tuple, stock_qty), items in groups.items():
        shop_configs = [
            {"id": sid, "warehouseId": warehouses[sid], "warehouseQuantity": stock_qty}
            for sid in target_tuple
        ]
        target_str = ",".join(map(str, target_tuple))

        for chunk_idx in range(0, len(items), BATCH_SIZE):
            curr_chunk += 1
            chunk = items[chunk_idx:chunk_idx + BATCH_SIZE]
            sku_prices = [
                {
                    "sku": it["sku"],
                    "price": it["price_str"],
                    "packageLength": it["packageLength"],
                    "packageWidth": it["packageWidth"],
                    "packageHeight": it["packageHeight"],
                    "weightBrut": it["weightBrut"],
                    "vendorCodePrefix": it["vendorCodePrefix"],
                }
                for it in chunk
            ]

            print(f"\n[批次 {curr_chunk}/{total_chunks}] 推送 {len(chunk)} 个商品 → 店[{target_str}]（库存={stock_qty}）...")
            try:
                resp = bcs.batch_push_products(shop_configs, sku_prices, mode=1, carry_brand=1)
                now = datetime.now().strftime("%H:%M:%S")
                if resp.get("code") == 200:
                    task_id = (resp.get("data") or {}).get("taskId") or "已提交"
                    print(f"  ✓ 批次提交成功 (taskId={task_id})")
                    for it in chunk:
                        ok += 1
                        writer.writerow([now, it["k"], it["nm"], it["cn"], it["item"]["vc"],
                                         it["vendorCodePrefix"], it["prefix_from"], target_str,
                                         it["price_str"], it["stock"], "成功", f"taskId={task_id}"])
                        for sid in it["valid_targets"]:
                            # 本地记录按原始 WB 码 k 记录，确保跨账号防重有效
                            records.setdefault(it["k"], {})[str(sid)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    replicate._save_records(records)
                else:
                    msg = resp.get("msg") or f"code={resp.get('code')}"
                    print(f"  ✗ 批次提交失败: {msg}")
                    for it in chunk:
                        fail += 1
                        writer.writerow([now, it["k"], it["nm"], it["cn"], it["item"]["vc"],
                                         it["vendorCodePrefix"], it["prefix_from"], target_str,
                                         it["price_str"], it["stock"], "失败", msg])
            except Exception as e:
                now = datetime.now().strftime("%H:%M:%S")
                print(f"  ✗ 批次请求异常: {e}")
                for it in chunk:
                    fail += 1
                    writer.writerow([now, it["k"], it["nm"], it["cn"], it["item"]["vc"],
                                     it["vendorCodePrefix"], it["prefix_from"], target_str,
                                     it["price_str"], it["stock"], "失败", str(e)])

            if curr_chunk < total_chunks:
                time.sleep(getattr(args, "interval", 1.0))

    csv_file.close()
    print(f"\n[汇总] 计划 {len(plans)} | 成功 {ok} | 跳过 {skip} | 失败 {fail}（{time.time() - t0:.0f}s）")
    print(f"明细：{csv_path}")

    # ---- 写后验证：仅加 --sync 时 fetch 同步复核（否则只打印提示，不自动做）----
    if ok > 0 and getattr(args, "sync", False) and not args.no_verify:
        print("\n[写后验证] 触发全店同步 + 拉取（~1.5 分钟）...")
        try:
            products.fetch_all()
            shops_data2, _ = products.load_all_shops()
            vc_shops2, _, _ = replicate.build_coverage(shops_data2)
            per_shop = {sid: len([1 for s in vc_shops2.values() if sid in s]) for sid in all_shop_ids}
            print(f"[验证] 各店在架 vc 数：{per_shop}")
        except Exception as e:
            print(f"[验证] 失败：{e}（可稍后手动 wb.py fetch 复核）")
        from . import mapping_sync
        mapping_sync.post_write_merge(fetch=False)
    elif ok > 0:
        from . import mapping_sync
        mapping_sync.print_write_hint()
    return 0

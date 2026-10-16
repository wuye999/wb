# -*- coding: utf-8 -*-
"""
wb_ops 商品链接评分与买家评价查询 (ratings)

通过 WB 卖家后台官方「内容分析 - 商品评分与评价」接口查询各店铺在架商品的评分、
评价数及各星级分布，并与权威商品价格表（data/商品价格表.xlsx）级联匹配，
导出包含 BCS编号、wb编号、中文名、售价、尺寸、前缀码等维度的统计报表。

用法：
    python wb.py ratings [--shops 9352] [--rating 1] [--one-star] [--max-rating 2] [--out path.xlsx]
"""
import csv
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ... import common, config, credentials
from ...adapters import wb_client as wb_api
from ...adapters import wb_feedback_client as fbc
from ...domain.models import ProductRatingItem
from ...services.replicate.wb_card import extract_vc_prefix
from ...storage.mapping_repo import MappingRepository
from ...storage.nm_resolver import NmResolver

SHOP_SLEEP = 0.5

# 报表表头定义（严格对齐用户需求前8列，后续附加详细分析列）
REPORT_HEADERS = [
    "BCS编号",
    "wb编号",
    "wb编号来源",
    "产品中文名",
    "售价",
    "尺寸",
    "vendorCode前缀码",
    "来源的店铺",
    "评分",
    "评价总数",
    "1星数",
    "2星数",
    "3星数",
    "4星数",
    "5星数",
    "商品名称",
    "商品链接",
]


def _match_price_table(
    vc: str,
    nm_id: int,
    shop_id: int,
    boss_details: Dict[str, Dict[str, Any]],
    resolve_cn: Any,
    nm_resolver: Optional[NmResolver] = None,
) -> Dict[str, Any]:
    """级联匹配商品价格表数据。

    优先级：
    1. vendorCode 4位前缀码 -> boss_details["by_prefix"]
    2. resolve_cn(vc) (结合 vc_override + vc_known) -> boss_details["by_cn"]
    3. 本地快照 nmResolver 反查 vc -> 再匹配
    """
    by_prefix = boss_details.get("by_prefix") or {}
    by_cn = boss_details.get("by_cn") or {}

    pref = extract_vc_prefix(vc)
    if pref and pref in by_prefix:
        return by_prefix[pref]

    cn = resolve_cn(vc, "") if resolve_cn else ""
    if cn and cn in by_cn:
        return by_cn[cn]

    # 包含匹配 (模糊降级)
    if cn:
        for k, v in by_cn.items():
            if cn == k or cn in k or k in cn:
                return v

    if nm_resolver:
        res = nm_resolver.resolve(nm_id)
        resolved_vc = res.get("vc", "")
        resolved_cn = res.get("cn", "")
        if resolved_vc:
            r_pref = extract_vc_prefix(resolved_vc)
            if r_pref and r_pref in by_prefix:
                return by_prefix[r_pref]
        if resolved_cn and resolved_cn in by_cn:
            return by_cn[resolved_cn]


    return {
        "sku": "",
        "cn": cn or "",
        "dp": None,
        "dim": "",
        "price": None,
        "prefix": pref or "",
    }


def _item_matches_filter(item: ProductRatingItem, args: Any) -> bool:
    """判定单个商品是否满足全部筛选条件"""
    # 评价数门槛 (默认 >= 1)
    min_feedbacks = getattr(args, "min_feedbacks", 1)
    if min_feedbacks is not None and item.feedback_count < min_feedbacks:
        return False

    # 1 星筛选
    if getattr(args, "one_star", False) and item.one_star <= 0:
        return False
    min_one_star = getattr(args, "min_one_star", 0)
    if min_one_star > 0 and item.one_star < min_one_star:
        return False

    # 评分精确匹配 (如 --rating 1 或 1.0)
    exact_rating = getattr(args, "rating", None)
    if exact_rating is not None:
        if abs(item.feedback_rating - float(exact_rating)) > 0.05:
            return False

    # 评分区间
    min_rating = getattr(args, "min_rating", None)
    if min_rating is not None and item.feedback_rating < float(min_rating):
        return False
    max_rating = getattr(args, "max_rating", None)
    if max_rating is not None and item.feedback_rating > float(max_rating):
        return False

    # 中文名模糊筛选
    name_kw = getattr(args, "name", "")
    if name_kw and name_kw not in (item.cn_name or ""):
        return False

    # 前缀码筛选
    prefix_kw = getattr(args, "prefix", "")
    if prefix_kw and prefix_kw.upper() != (item.prefix or "").upper():
        return False

    # vc 筛选
    vc_kw = getattr(args, "vc", "")
    if vc_kw and vc_kw.upper() not in (item.vendor_code or "").upper():
        return False

    return True


def _export_csv(items: List[ProductRatingItem], csv_path: str) -> None:
    """导出为标准 UTF-8-SIG CSV 文件"""
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(REPORT_HEADERS)
        for it in items:
            wb_url = f"https://www.wildberries.ru/catalog/{it.nm_id}/detail.aspx"
            writer.writerow([
                it.vendor_code,
                it.nm_id,
                it.source_wb,
                it.cn_name,
                it.price if it.price is not None else "",
                it.dimensions,
                it.prefix,
                it.shop_name,
                it.feedback_rating,
                it.feedback_count,
                it.one_star,
                it.two_star,
                it.three_star,
                it.four_star,
                it.five_star,
                it.name,
                wb_url,
            ])


def _export_excel(items: List[ProductRatingItem], xlsx_path: str) -> None:
    """导出为具备格式美化与冻结首行的 Excel 文件"""
    os.makedirs(os.path.dirname(os.path.abspath(xlsx_path)), exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "商品评分明细"
    ws.append(REPORT_HEADERS)

    for it in items:
        wb_url = f"https://www.wildberries.ru/catalog/{it.nm_id}/detail.aspx"
        ws.append([
            it.vendor_code,
            it.nm_id,
            it.source_wb,
            it.cn_name,
            it.price if it.price is not None else "",
            it.dimensions,
            it.prefix,
            it.shop_name,
            it.feedback_rating,
            it.feedback_count,
            it.one_star,
            it.two_star,
            it.three_star,
            it.four_star,
            it.five_star,
            it.name,
            wb_url,
        ])

    header_font = Font(name="微软雅黑", size=10, bold=True)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    cell_font = Font(name="微软雅黑", size=9)

    for col in range(1, len(REPORT_HEADERS) + 1):
        c = ws.cell(row=1, column=col)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", vertical="center")

    for row in range(2, len(items) + 2):
        for col in range(1, len(REPORT_HEADERS) + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = cell_font
            # 居中对齐代码与数值列，文本靠左
            if col in [1, 2, 3, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15]:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")

    ws.freeze_panes = "A2"
    for i, name in enumerate(REPORT_HEADERS, 1):
        max_len = max(len(str(name)) * 2 + 4, 12)
        ws.column_dimensions[get_column_letter(i)].width = min(max_len, 30)

    wb.save(xlsx_path)


def run(args: Any) -> int:
    """商品链接评分查询与报表导出主入口"""
    common.ensure_utf8_stdout()

    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已配置可用 cookie 的店铺")
        return 1

    pairs = [(s, common.to_int(s.get("shopId"))) for s in wb_shops]
    if getattr(args, "shops", ""):
        want = {int(x.strip()) for x in args.shops.split(",") if x.strip()}
        pairs = [p for p in pairs if p[1] in want]

    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 参数或 credentials.json）")
        return 1

    shop_desc = ", ".join(f"{s['shopName']}({sid})" for s, sid in pairs)
    days = getattr(args, "days", 365) or 365
    start_d, end_d = fbc.default_period_dates(days)
    print(f"==== WB 商品链接评分查询 ====")
    print(f"目标店铺 ({len(pairs)} 个): {shop_desc}")
    print(f"统计周期: {start_d} 至 {end_d} (近 {days} 天)")

    # 预载入价格表与名称解析器
    boss_details = MappingRepository.load_boss_details()
    resolve_cn, _ = MappingRepository.build_vc_resolver()

    # 智能排序优化：若筛选 1 星，则按 oneStar desc 排序拉取；若看评价数，按 feedbackCount desc
    order_field = "feedbackRating"
    order_mode = "desc"
    stop_one_star = False
    stop_feedback = False

    if getattr(args, "one_star", False) or getattr(args, "min_one_star", 0) > 0:
        order_field = "oneStar"
        order_mode = "desc"
        stop_one_star = True
    elif getattr(args, "rating", None) is not None and float(args.rating) <= 1.0:
        # 评分为 1 的商品
        order_field = "oneStar"
        order_mode = "desc"

    matched_items: List[ProductRatingItem] = []
    total_scanned = 0

    for shop, sid in pairs:
        name = shop.get("shopName") or f"shop_{sid}"
        print(f"\n正在查询店铺: {name} (ID: {sid})...")
        try:
            session = wb_api.make_session(shop, cred.root_version)
            nm_resolver = NmResolver(sid)

            shop_scanned = 0
            shop_matched = 0

            for raw in fbc.iter_feedback_products(
                session=session,
                start_date=start_d,
                end_date=end_d,
                order_by_field=order_field,
                order_by_mode=order_mode,
                page_size=100,
                limit=getattr(args, "limit", 0) or 0,
                stop_on_zero_one_star=stop_one_star,
                stop_on_zero_feedback=stop_feedback,
            ):
                shop_scanned += 1
                total_scanned += 1

                nm_id = common.to_int(raw.get("nmId"))
                vc = str(raw.get("vendorCode") or "").strip()
                p_name = str(raw.get("name") or "").strip()
                fb_rating = float(raw.get("feedbackRating", {}).get("current", 0) or 0)
                fb_count = common.to_int(raw.get("feedbackCount", {}).get("current", 0))
                one_s = common.to_int(raw.get("oneStar", {}).get("current", 0))
                two_s = common.to_int(raw.get("twoStar", {}).get("current", 0))
                three_s = common.to_int(raw.get("threeStar", {}).get("current", 0))
                four_s = common.to_int(raw.get("fourStar", {}).get("current", 0))
                five_s = common.to_int(raw.get("fiveStar", {}).get("current", 0))
                quality_rating = float(raw.get("rating") or 0)
                is_shadowed = bool(raw.get("isShadowed", False))

                # 匹配价格表
                p_info = _match_price_table(
                    vc=vc,
                    nm_id=nm_id,
                    shop_id=sid,
                    boss_details=boss_details,
                    resolve_cn=resolve_cn,
                    nm_resolver=nm_resolver,
                )

                item = ProductRatingItem(
                    shop_id=sid,
                    shop_name=name,
                    nm_id=nm_id,
                    vendor_code=vc,
                    name=p_name,
                    rating=quality_rating,
                    feedback_rating=fb_rating,
                    feedback_count=fb_count,
                    one_star=one_s,
                    two_star=two_s,
                    three_star=three_s,
                    four_star=four_s,
                    five_star=five_s,
                    is_shadowed=is_shadowed,
                    cn_name=p_info.get("cn") or "",
                    price=p_info.get("dp"),  # 售价取价格表双倍售价
                    dimensions=p_info.get("dim") or "",
                    prefix=p_info.get("prefix") or "",
                    source_wb="商品评分API",
                )

                if _item_matches_filter(item, args):
                    matched_items.append(item)
                    shop_matched += 1

            print(f"  → 本店扫描: {shop_scanned} 件，命中筛选: {shop_matched} 件")
            time.sleep(SHOP_SLEEP)

        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {name} Cookie 已失效: {e}（跳过该店）")
            continue
        except Exception as e:
            print(f"  [警告] 店铺 {name} 查询失败: {e}（跳过该店）")
            continue

    print(f"\n==========================================")
    print(f"【汇总统计】共扫描 {total_scanned} 件商品，命中筛选 {len(matched_items)} 件")
    print(f"==========================================")

    if not matched_items:
        print("未检索到符合条件的商品。")
        return 0

    # 控制台预览前 10 条
    print("\n[预览前 10 条结果]")
    print(f"{'BCS编号':<22} | {'wb编号':<12} | {'产品中文名':<18} | {'售价':<6} | {'评分':<5} | {'总评':<5} | {'1星':<4} | {'店铺':<6}")
    print("-" * 90)
    for it in matched_items[:10]:
        price_str = str(it.price) if it.price is not None else "-"
        cn_disp = it.cn_name[:16] if it.cn_name else "-"
        print(f"{it.vendor_code:<22} | {it.nm_id:<12} | {cn_disp:<18} | {price_str:<6} | {it.feedback_rating:<5.2f} | {it.feedback_count:<5} | {it.one_star:<4} | {it.shop_name:<6}")

    if len(matched_items) > 10:
        print(f"... 剩余 {len(matched_items) - 10} 条已保存到报表文件。")

    # 导出文件
    out_opt = getattr(args, "out", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(config.LOG_DIR, exist_ok=True)

    if out_opt:
        if out_opt.lower().endswith(".xlsx"):
            _export_excel(matched_items, out_opt)
            print(f"\n[报表已导出] Excel: {out_opt}")
        else:
            _export_csv(matched_items, out_opt)
            print(f"\n[报表已导出] CSV: {out_opt}")
    else:
        # 默认同时导出 CSV 与 XLSX 到 data/logs/
        csv_file = os.path.join(config.LOG_DIR, f"ratings_{ts}.csv")
        xlsx_file = os.path.join(config.LOG_DIR, f"ratings_{ts}.xlsx")
        _export_csv(matched_items, csv_file)
        _export_excel(matched_items, xlsx_file)
        print(f"\n[报表已自动导出]")
        print(f"  - CSV : {csv_file}")
        print(f"  - Excel: {xlsx_file}")

    return 0

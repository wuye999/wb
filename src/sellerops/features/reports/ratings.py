"""从 WB Open API 三路评价明细统计商品链接的窗口评分。"""
from collections import Counter, defaultdict
from contextlib import closing
import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
import os
from typing import Any, List

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from sellerops.core import utils as common
from sellerops.core.errors import AuthenticationError, NetworkError, RateLimitError
from sellerops.domain.models import ProductRatingItem
from sellerops.domain.snapshot_values import positive_id
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.features.catalog.shops import archive_set
from sellerops.features.reports.product_report_data import TZ, timestamp
from sellerops.integrations.bcs.client import BCSClient, OpenAPIShop, sync_failure_reason
from sellerops.integrations.wildberries.open_api import WBOpenAPI, OpenAPIResponseError
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.settings import paths as config

# 报表表头定义（严格对齐用户需求前8列，后续附加详细分析列）
REPORT_HEADERS = [
    "BCS编号",
    "wb编号",
    "wb编号来源",
    "产品中文名",
    "最低售价",
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


def _match_price_table(vc: str, entries: dict) -> dict:
    if not vc:
        return {}
    row = db.resolve_vc(vc, entries)
    if row['status'] == 'mapped':
        return {**row, 'dim': row['dimensions']}
    return {'cn': '\u975e\u8d27\u76d8' if row['status'] == 'excluded' else '\u5f85\u8ba4\u9886',
            'prefix': extract_vc_prefix(vc) or ''}


def _item_matches_filter(item: ProductRatingItem, args: Any, stars: set[int]) -> bool:
    """判定单个商品是否满足全部筛选条件"""
    # 评价数门槛 (默认 >= 1)
    if item.feedback_count < args.min_feedbacks:
        return False

    # 差评筛选 (1星 或 2星)
    if args.negative and item.one_star + item.two_star == 0:
        return False

    # 1 星筛选
    if item.one_star < max(args.min_one_star, int(args.one_star)):
        return False

    # 2 星筛选
    if args.two_star and item.two_star == 0:
        return False

    # 指定星级评论筛选 (如 --stars 1,2)
    counts = (item.one_star, item.two_star, item.three_star, item.four_star, item.five_star)
    if stars and not any(counts[star - 1] for star in stars):
        return False

    if not item.feedback_count and any(value is not None for value in (args.rating, args.min_rating, args.max_rating)):
        return False
    if args.rating is not None and round(abs(item.feedback_rating - args.rating), 12) > 0.05:
        return False
    if args.min_rating is not None and item.feedback_rating < args.min_rating:
        return False
    if args.max_rating is not None and item.feedback_rating > args.max_rating:
        return False

    if args.name and args.name != item.cn_name:
        return False
    if args.name_contains and args.name_contains not in item.cn_name:
        return False
    if args.prefix and args.prefix.upper() != item.prefix.upper():
        return False
    if args.vc and args.vc.upper() not in item.vendor_code.upper():
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


class RatingsDataError(ValueError):
    """评价明细结构无效，消息不含远端字段内容。"""


@dataclass(slots=True)
class FeedbackCounts:
    stars: Counter[int] = field(default_factory=Counter)
    vendor_codes: set[str] = field(default_factory=set)
    title: str = ""
    title_date: datetime | None = None


def _feedback_window(days: int, now: datetime | None = None) -> tuple[datetime | None, datetime]:
    if days < 0:
        raise ValueError("--days 必须大于或等于 0")
    today = timestamp(now or datetime.now(TZ)).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return (today - timedelta(days=days - 1) if days else None, today + timedelta(days=1))
    except OverflowError:
        raise ValueError("--days 超出可表示的日期范围") from None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _group_feedbacks(rows: list[dict], start: datetime | None, end: datetime) -> dict[int, FeedbackCounts]:
    grouped: dict[int, FeedbackCounts] = defaultdict(FeedbackCounts)
    for raw in rows:
        try:
            created = timestamp(raw.get("createdDate"))
        except (AttributeError, ValueError, TypeError, OverflowError, OSError):
            raise RatingsDataError("评价缺少可解析的创建时间") from None
        if created >= end or (start is not None and created < start):
            continue
        details = raw.get("productDetails")
        nm_id = positive_id(details.get("nmId")) if isinstance(details, dict) else None
        stars = raw.get("productValuation")
        if nm_id is None or isinstance(stars, bool) or stars not in (1, 2, 3, 4, 5):
            raise RatingsDataError("窗口内评价缺少有效商品码或星级")
        group = grouped[nm_id]
        group.stars[int(stars)] += 1
        vc = _text(details.get("supplierArticle"))
        if vc:
            group.vendor_codes.add(vc)
        title = _text(details.get("productName"))
        if title and (group.title_date is None or created > group.title_date):
            group.title, group.title_date = title, created
    return grouped


def _snapshot_index(sid: int) -> tuple[dict[int, dict], str, str]:
    try:
        document = ProductSnapshotRepository.load_snapshot_document(sid)
    except Exception as error:
        return {}, "", f"本店快照读取失败（{type(error).__name__}）"
    if document is None:
        return {}, "", "本店商品快照缺失"
    indexed = {}
    invalid = 0
    for row in document["rows"]:
        nm_id = positive_id(row.get("nmId") or row.get("nmID"))
        if nm_id is not None:
            indexed[nm_id] = row
        elif not row.get("trashedAt"):
            invalid += 1
    issue = f"本店快照有 {invalid} 个在架商品缺少有效商品码" if invalid else ""
    return indexed, str(document.get("fetchedAt") or "未记录"), issue


def _shop_ratings(shop: OpenAPIShop, grouped: dict[int, FeedbackCounts], entries: dict,
                  include_zero: bool) -> tuple[list[ProductRatingItem], str]:
    snapshot = {}
    snapshot_issue = ""
    if include_zero or any(not group.vendor_codes or not group.title for group in grouped.values()):
        snapshot, fetched_at, snapshot_issue = _snapshot_index(shop.id)
        if include_zero:
            print(f"  [零评价范围] 本店在架快照时间 {fetched_at or '不可用'}")
        if snapshot_issue:
            print(f"  [警告] {snapshot_issue}")
    nm_ids = set(grouped)
    if include_zero:
        nm_ids.update(nm for nm, row in snapshot.items() if not row.get("trashedAt"))
    items = []
    conflicts = 0
    for nm_id in sorted(nm_ids):
        group = grouped.get(nm_id) or FeedbackCounts()
        local = snapshot.get(nm_id, {})
        if len(group.vendor_codes) > 1:
            conflicts += 1
            vc = ""
        else:
            vc = next(iter(group.vendor_codes), "") or _text(local.get("vendorCode") or local.get("goodsCode"))
        title = group.title or _text(local.get("title"))
        counts = group.stars
        count = sum(counts.values())
        average = sum(star * amount for star, amount in counts.items()) / count if count else 0.0
        info = _match_price_table(vc, entries)
        items.append(ProductRatingItem(
            shop_id=shop.id, shop_name=shop.name, nm_id=nm_id, vendor_code=vc, name=title,
            feedback_rating=average, feedback_count=count, one_star=counts[1], two_star=counts[2],
            three_star=counts[3], four_star=counts[4], five_star=counts[5], cn_name=info.get("cn") or "",
            price=info.get("minimum_price"), dimensions=info.get("dim") or "", prefix=info.get("prefix") or "",
            source_wb="WB Open API 评价" if count else "本店快照",
        ))
    if conflicts:
        print(f"  [警告] {conflicts} 个商品在评价中有冲突的完整 VC，归属字段留空")
    return items, snapshot_issue if include_zero else ""


def _read_failure(error: Exception) -> str:
    if isinstance(error, AuthenticationError):
        return "WB 令牌失效或评价类别权限不足"
    if isinstance(error, RateLimitError):
        return "WB 评价接口限流"
    if isinstance(error, NetworkError):
        return "WB 评价接口网络读取失败"
    if isinstance(error, OpenAPIResponseError):
        return error.public_reason
    if isinstance(error, RatingsDataError):
        return str(error)
    return f"评分查询失败（{type(error).__name__}）"


def _product_order(args: Any) -> str:
    if args.negative or args.two_star or args.stars:
        return "feedback_count"
    if args.one_star or args.min_one_star > 0 or (args.rating is not None and args.rating <= 1.0):
        return "one_star"
    return "feedback_rating"


def run(args: Any) -> int:
    """商品链接评分查询与报表导出主入口。"""
    common.ensure_utf8_stdout()
    start, end = _feedback_window(args.days)
    through = (end - timedelta(days=1)).date().isoformat()
    since = start.date().isoformat() if start else None
    if args.limit < 0 or args.min_feedbacks < 0:
        raise ValueError("--limit 和 --min-feedbacks 必须大于或等于 0")
    if any(value is not None and not isfinite(value) for value in (args.rating, args.min_rating, args.max_rating)):
        raise ValueError("评分筛选须使用有限数值")
    star_tokens = [part.strip() for part in args.stars.split(",")] if args.stars else []
    if any(part not in {"1", "2", "3", "4", "5"} for part in star_tokens):
        raise ValueError("--stars 必须为 1–5 的逗号分隔星级")
    stars = {int(part) for part in star_tokens}
    shop_tokens = [part.strip() for part in args.shops.split(",")] if args.shops else []
    if any(not part.isascii() or not positive_id(part) for part in shop_tokens):
        raise ValueError("--shops 必须为逗号分隔的正整数店铺 ID")
    wanted = {int(part) for part in shop_tokens}
    failed_shops: list[dict] = []
    args.output = {"rows": 0, "failed_shops": failed_shops, "wb_region": args.wb_region,
                   "period": {"from": since, "through": through, "timezone": "UTC+8"}}

    try:
        directory = BCSClient(auto_refresh=False).fetch_open_api_shops()
    except Exception as error:
        args.summary = f"BCS 店铺目录读取失败：{sync_failure_reason(error)}"
        if isinstance(error, AuthenticationError):
            args.next_action = "通过 login bcs 更新本机 BCS 会话后再查询"
        print(f"[错误] {args.summary}")
        return 1

    archived = archive_set()
    shops = [shop for shop in directory if shop.id not in archived and (not wanted or shop.id in wanted)]
    for sid in sorted(wanted - {shop.id for shop in shops}):
        reason = "本地店铺已归档" if sid in archived else "BCS 店铺目录未找到该店"
        failed_shops.append({"shop_id": sid, "error": reason})
        print(f"[警告] 店铺 {sid}：{reason}")
    if not shops:
        args.summary = "BCS 目录中没有符合条件的在营店铺"
        print(f"[错误] {args.summary}")
        return 1
    shop_desc = ", ".join(f"{shop.name}({shop.id})" for shop in shops)
    period = f"{since} 至 {through}（近 {args.days} 天，含今天）" if since else f"接口可访问的全部历史至 {through}"
    print(f"==== WB 商品链接评分查询（{args.wb_region}） ====")
    print(f"目标店铺 ({len(shops)} 个): {shop_desc}")
    print(f"统计周期（UTC+8）: {period}")

    MappingRepository.load_price_book(require_price=False)
    entries = db.vc_entries()
    order_field = _product_order(args)
    date_from = int(start.timestamp()) - 1 if start is not None else None
    date_to = int(end.timestamp())
    matched_items: list[ProductRatingItem] = []
    total_scanned, complete_shops = 0, 0
    for shop in shops:
        print(f"\n正在查询店铺: {shop.name} (ID: {shop.id})...")
        if not shop.api_key:
            reason = "BCS 未配置本店 WB apiKey"
            failed_shops.append({"shop_id": shop.id, "error": reason})
            print(f"  [警告] {reason}")
            continue
        try:
            with closing(WBOpenAPI(shop.api_key, region=args.wb_region)) as api:
                raw_feedbacks = api.feedbacks(date_from=date_from, date_to=date_to)
            grouped = _group_feedbacks(raw_feedbacks, start, end)
            shop_items, snapshot_issue = _shop_ratings(shop, grouped, entries, args.min_feedbacks == 0)
        except Exception as error:
            reason = _read_failure(error)
            failed_shops.append({"shop_id": shop.id, "error": reason})
            print(f"  [警告] {reason}（该店中止）")
            continue
        if snapshot_issue:
            failed_shops.append({"shop_id": shop.id, "error": f"零评价商品覆盖不完整：{snapshot_issue}"})
        else:
            complete_shops += 1
        feedback_count = sum(item.feedback_count for item in shop_items)
        shop_items.sort(key=lambda item: (-getattr(item, order_field), item.nm_id))
        if args.limit:
            shop_items = shop_items[:args.limit]
        selected = [item for item in shop_items if _item_matches_filter(item, args, stars)]
        matched_items.extend(selected)
        total_scanned += len(shop_items)
        print(f"  → 本店窗口评价 {feedback_count} 条，进入筛选 {len(shop_items)} 件，命中 {len(selected)} 件")

    print(f"\n[汇总] 进入筛选 {total_scanned} 件商品，命中 {len(matched_items)} 件，覆盖异常 {len(failed_shops)} 店")
    if not matched_items:
        if not failed_shops:
            args.summary = "没有符合条件的评分记录"
        elif complete_shops:
            args.summary = "部分店铺覆盖不完整，已取得的数据无筛选命中"
        else:
            args.summary = "所选店铺均未取得完整评分数据"
        print(args.summary)
        return int(bool(failed_shops))

    # 控制台预览前 10 条
    print("\n[预览前 10 条结果]")
    print(f"{'BCS编号':<22} | {'wb编号':<12} | {'产品中文名':<18} | {'最低售价':<6} | {'评分':<5} | {'总评':<5} | {'1星':<4} | {'店铺':<6}")
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
    os.makedirs(str(config.current.exports_dir), exist_ok=True)

    if out_opt:
        if out_opt.lower().endswith(".xlsx"):
            _export_excel(matched_items, out_opt)
            artifacts = {"workbook": os.path.abspath(out_opt)}
            print(f"\n[报表已导出] Excel: {out_opt}")
        else:
            _export_csv(matched_items, out_opt)
            artifacts = {"csv": os.path.abspath(out_opt)}
            print(f"\n[报表已导出] CSV: {out_opt}")
    else:
        # 默认同时导出 CSV 与 XLSX 到 exports/
        csv_file = os.path.join(str(config.current.exports_dir), f"ratings_{ts}.csv")
        xlsx_file = os.path.join(str(config.current.exports_dir), f"ratings_{ts}.xlsx")
        _export_csv(matched_items, csv_file)
        _export_excel(matched_items, xlsx_file)
        artifacts = {"workbook": xlsx_file, "csv": csv_file}
        print(f"\n[报表已自动导出]")
        print(f"  - CSV : {csv_file}")
        print(f"  - Excel: {xlsx_file}")

    args.summary = "已导出商品评分报表，部分店铺覆盖不完整" if failed_shops else "已导出商品评分报表"
    args.output.update(rows=len(matched_items), artifacts=artifacts)
    return int(bool(failed_shops))

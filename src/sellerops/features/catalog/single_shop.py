"""single shop。"""
import os

import openpyxl
from sellerops.domain.snapshot_values import first_price
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.persistence.headers import COL_MIN_PRICE
from sellerops.persistence.headers import COL_CN
from sellerops.persistence.headers import COL_H
from sellerops.persistence.headers import COL_IMG
from sellerops.persistence.headers import COL_L

from sellerops.persistence.headers import COL_TITLE
from sellerops.persistence.headers import COL_VC
from sellerops.persistence.headers import COL_W
from sellerops.persistence.headers import COL_WT
from sellerops.features.catalog.mapping_excel import stock_summary

SINGLE_SHOP_HEADERS = [
    COL_CN, COL_VC, COL_MIN_PRICE, '本店售价(CNY)', '折扣%', 'club折扣%',
    '本店库存', '本店nmId', '俄文标题', '主图链接',
    '尺寸长(cm)', '尺寸宽(cm)', '尺寸高(cm)', '毛重(kg)',
    '创建时间', '更新时间', '匹配类型'
]

SINGLE_SHOP_WIDTHS = [20, 24, 10, 12, 8, 10, 10, 14, 32, 14, 10, 10, 10, 10, 18, 18, 12]

SINGLE_UNMAPPED_HEADERS = [
    'vendorCode', '俄文标题', '本店售价(CNY)', '折扣%', '本店库存', '本店nmId', '主图链接', '创建时间'
]

SINGLE_UNMAPPED_WIDTHS = [24, 32, 12, 8, 10, 14, 14, 18]

def build_single_shop_xlsx(sid: int, out_path):
    """构建单店铺映射表（包含该店在架商品、本店售价、库存和 nmId 等）。
    包含 2 个 Sheet：
      1. 在架映射明细（已归属中文名的商品）
      2. 未映射在架商品（在架但未匹配到中文名的商品）
    """
    document = ProductSnapshotRepository.load_snapshot_document(sid)
    if document is None:
        raise ValueError(f'店铺 {sid} 缺少快照')
    MappingRepository.load_price_book(require_price=False)
    entries = db.vc_entries()
    unique_rows = [row for row in document['rows'] if not row.get('trashedAt')]
    out_file = out_path
    os.makedirs(os.path.dirname(out_file) or '.', exist_ok=True)

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "在架映射明细"
    ws1.append(SINGLE_SHOP_HEADERS)

    ws2 = wb.create_sheet("未映射在架商品")
    ws2.append(SINGLE_UNMAPPED_HEADERS)

    n_mapped, n_unmapped = 0, 0
    for r in unique_rows:
        vc = r.get("vendorCode")
        if not vc:
            continue
        price = first_price(r)
        disc = r.get("discount")
        club = r.get("clubDiscount")
        stock = stock_summary(r)
        nm_id = r.get("nmId")
        title = r.get("title") or ""
        img = r.get("repImg") or ""
        L = r.get("dimensionsLength")
        W = r.get("dimensionsWidth")
        H = r.get("dimensionsHeight")
        weight = r.get("dimensionsWeightBrutto")
        create_at = r.get("createAt")
        update_at = r.get("updateAt")

        item = db.resolve_vc(vc, entries)
        if item['status'] == 'excluded':
            continue
        cn, minimum_price = item.get('cn'), item.get('minimum_price')
        match_type = item.get('source') or ''

        if cn:
            ws1.append([
                cn, vc, minimum_price, price, disc, club, stock, nm_id, title, img,
                L, W, H, weight, create_at, update_at, match_type
            ])
            cur_row = ws1.max_row
            if img:
                ws1.cell(cur_row, 10).hyperlink = img
            if nm_id:
                ws1.cell(cur_row, 8).hyperlink = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx?targetUrl=GP"
            n_mapped += 1
        else:
            ws2.append([vc, title, price, disc, stock, nm_id, img, create_at])
            cur_row = ws2.max_row
            if img:
                ws2.cell(cur_row, 7).hyperlink = img
            if nm_id:
                ws2.cell(cur_row, 6).hyperlink = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx?targetUrl=GP"
            n_unmapped += 1

    for i, w in enumerate(SINGLE_SHOP_WIDTHS, 1):
        ws1.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws1.freeze_panes = "C2"

    for i, w in enumerate(SINGLE_UNMAPPED_WIDTHS, 1):
        ws2.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws2.freeze_panes = "B2"

    wb.save(out_file)
    wb.close()

    return n_mapped, n_unmapped

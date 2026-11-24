"""从本地快照离线导出按源数字码去重的标准商品表。"""
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from sellerops.domain.snapshot_values import first_price, positive_id, stock_values
from sellerops.domain.vendor_codes import extract_vc_source_code
from sellerops.features.reports.product_report_data import product_url
from sellerops.persistence import db
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.settings import paths


HEADERS = ('来源店铺', '商品中文名', 'vendorCode', '源数字码', '本店nmId', '售价(CNY)',
           '长(cm)', '宽(cm)', '高(cm)', '毛重(kg)', '库存', '俄文标题', '主图链接')


def _public_image_url(value: str) -> str:
    """保留公开图片路径，避免把 URL 中的认证参数带入交换表。"""
    try:
        url = urlsplit(value)
    except ValueError:
        return ''
    if url.scheme in {'http', 'https'} and url.hostname and url.username is None:
        return url._replace(query='', fragment='').geturl()
    return ''


def _positive_dimension(value) -> bool:
    try:
        number = float(str(value))
    except ValueError:
        return False
    return isfinite(number) and number > 0


def export_products(shop_ids: list[int] | None, out_path: str | Path) -> dict:
    """只读导出指定店铺，shop_ids 为 None 时使用全部活跃店铺。"""
    target = Path(out_path).expanduser().resolve()
    if target.suffix.lower() != '.xlsx':
        raise ValueError('输出文件必须为 .xlsx')
    if any(target.is_relative_to(folder.resolve()) for folder in
           (Path(paths.current.data_dir), paths.PROJECT_ROOT / 'data')):
        raise ValueError('请将交换表导出到数据目录之外')

    representatives = {}
    scanned = filtered = 0
    with db.connect(read_only=True) as connection:
        active_shops = {int(row['shop_id']): row['shop_name'] for row in connection.execute(
            "SELECT shop_id, shop_name FROM shops WHERE status = 'active'")}
        selected = sorted(active_shops if shop_ids is None else shop_ids)
        entries = db.vc_entries(connection)
        for shop_id in selected:
            document = ProductSnapshotRepository.load_snapshot_document(shop_id, connection=connection)
            if document is None:
                raise ValueError(f'店铺 {shop_id} 缺少本地快照')
            shop_name = active_shops.get(shop_id) or str(shop_id)
            scanned += len(document['rows'])
            for index, row in enumerate(document['rows']):
                vc = str(row.get('vendorCode') or row.get('goodsCode') or '').strip()
                entry = entries.get(vc, {})
                if row.get('trashedAt') or entry.get('status') == 'excluded':
                    filtered += 1
                    continue
                source = extract_vc_source_code(vc, min_digits=6) or vc
                key = source or (shop_id, index)
                price = first_price(row)
                dims = tuple(row.get(field) for field in ('dimensionsLength', 'dimensionsWidth',
                                                           'dimensionsHeight', 'dimensionsWeightBrutto'))
                complete = price is not None and price > 0 and all(map(_positive_dimension, dims))
                rank = (not complete, shop_id != paths.MAIN_SHOP, shop_id)
                if key in representatives and representatives[key][0] <= rank:
                    continue
                nm_id = positive_id(row.get('nmId') or row.get('nmID'))
                stocks = stock_values(row)
                image = _public_image_url(str(row.get('repImg') or ''))
                values = (shop_name, entry.get('cn'), vc, source, nm_id, price, *dims,
                          sum(stocks) if stocks is not None else None, row.get('title'), image)
                representatives[key] = (rank, values)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '商品列表'
    sheet.append(HEADERS)
    sheet.freeze_panes = 'A2'
    for col, width in enumerate((22, 26, 31, 19, 16, 14, 12, 12, 12, 14, 10, 46, 62), 1):
        sheet.column_dimensions[get_column_letter(col)].width = width

    for _, values in representatives.values():
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'
        for column, url in ((5, product_url(values[4])), (13, values[-1])):
            if url:
                cell = sheet.cell(sheet.max_row, column)
                cell.hyperlink = url
                cell.style = 'Hyperlink'

    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    count = len(representatives)
    return {'shop_count': len(selected), 'scanned': scanned, 'filtered': filtered,
            'duplicates': scanned - filtered - count, 'count': count, 'out_path': str(target)}


def run(args) -> int:
    shop_ids = None if args.all_shops else [int(sid) for sid in args.shops.split(',')]
    filename = f'shop_{shop_ids[0]}_products.xlsx' if shop_ids and len(shop_ids) == 1 else 'shops_products.xlsx'
    result = export_products(shop_ids, args.out or paths.current.exports_dir / filename)
    print(f"店铺 {result['shop_count']} 家，扫描 {result['scanned']} 条，过滤 {result['filtered']} 条，"
          f"去重跳过 {result['duplicates']} 条，最终导出 {result['count']} 条")
    print(f"导出文件 {result['out_path']}")
    return 0

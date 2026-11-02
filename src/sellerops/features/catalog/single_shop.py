"""single shop。"""
import glob
import os
import re

import openpyxl
from sellerops.settings import paths as config
from sellerops.persistence.headers import COL_ACTUAL_PRICE
from sellerops.persistence.headers import COL_CN
from sellerops.persistence.headers import COL_H
from sellerops.persistence.headers import COL_IMG
from sellerops.persistence.headers import COL_L

from sellerops.persistence.headers import COL_TITLE
from sellerops.persistence.headers import COL_VC
from sellerops.persistence.headers import COL_W
from sellerops.persistence.headers import COL_WT
from sellerops.persistence.headers import find_col
from sellerops.persistence.headers import warn_missing
from sellerops.features.catalog.mapping_excel import stock_summary

SINGLE_SHOP_HEADERS = [
    COL_CN, COL_VC, COL_ACTUAL_PRICE, '本店售价(CNY)', '折扣%', 'club折扣%',
    '本店库存', '本店nmId', '俄文标题', '主图链接',
    '尺寸长(cm)', '尺寸宽(cm)', '尺寸高(cm)', '毛重(kg)',
    '创建时间', '更新时间', '匹配类型'
]

SINGLE_SHOP_WIDTHS = [20, 24, 10, 12, 8, 10, 10, 14, 32, 14, 10, 10, 10, 10, 18, 18, 12]

SINGLE_UNMAPPED_HEADERS = [
    'vendorCode', '俄文标题', '本店售价(CNY)', '折扣%', '本店库存', '本店nmId', '主图链接', '创建时间'
]

SINGLE_UNMAPPED_WIDTHS = [24, 32, 12, 8, 10, 14, 14, 18]

def build_single_shop_xlsx(sid, shop_name=None, rows=None, registry=None, boss=None, prefix_map=None, out_path=None):
    """构建单店铺映射表（包含该店真实在架商品、实际售价、实际库存、实际nmId等）。
    包含 2 个 Sheet：
      1. 在架映射明细（已归属中文名的商品）
      2. 未映射在架商品（在架但未匹配到中文名的商品）
    """
    if rows is None:
        p_json = config.shop_json_path(sid)
        if not os.path.exists(p_json):
            print(f"  [警告] 店铺 {sid} 无商品快照：{p_json}，跳过构建单店表")
            return 0, 0
        from sellerops.core.files import safe_load_json
        d = safe_load_json(p_json, default={})
        rows = d.get("rows", [])

    if registry is None:
        from sellerops.persistence.mappings import load_vc_known
        from sellerops.persistence.mappings import load_vc_override
        from sellerops.persistence.mappings import load_vc_excluded
        known = load_vc_known()
        overrides = load_vc_override()
        excluded = load_vc_excluded()
    else:
        known, overrides, excluded = registry

    if boss is None:
        from sellerops.persistence.mappings import load_price_book
        boss = load_price_book()
    boss_by_cn = {b["cn"]: b for b in boss if b.get("cn")}

    if prefix_map is None:
        from sellerops.persistence.mappings import load_prefix_map
        prefix_map = load_prefix_map()

    out_file = out_path or config.shop_mapping_xlsx(sid, shop_name)
    os.makedirs(os.path.dirname(out_file), exist_ok=True)

    alive_rows = [r for r in rows if not r.get("trashedAt")]

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "在架映射明细"
    ws1.append(SINGLE_SHOP_HEADERS)

    ws2 = wb.create_sheet("未映射在架商品")
    ws2.append(SINGLE_UNMAPPED_HEADERS)

    n_mapped, n_unmapped = 0, 0
    known_updated = False
    for r in alive_rows:
        vc = r.get("vendorCode")
        if not vc:
            continue
        sl = r.get("sizeList") or []
        price = sl[0].get("price") if sl else None
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

        cn = ""
        dp = None
        match_type = ""

        # 匹配归属优先级：overrides > known > prefix_map
        if vc in overrides:
            cn = overrides[vc].get("cn") or ""
            dp = overrides[vc].get("dp")
            match_type = "人工纠偏"
        elif vc in known:
            cn = known[vc].get("cn") or ""
            dp = known[vc].get("dp")
            match_type = "已归属"
            if L is None and known[vc].get("L"):
                L, W, H, weight = known[vc].get("L"), known[vc].get("W"), known[vc].get("H"), known[vc].get("weight")
        else:
            m = re.match(config.VC_PREFIX_RE, vc or "")
            if m:
                pfx = m.group(1)
                boss_row = prefix_map.get(pfx)
                if boss_row:
                    cn = boss_row.get("cn") or ""
                    dp = boss_row.get("dp")
                    match_type = "前缀自动"
                    known[vc] = {"cn": cn, "dp": dp, "sku": boss_row.get("sku", ""), "source": "前缀自动"}
                    known_updated = True

        # 用 boss 最新售价覆盖实际售价
        if cn in boss_by_cn:
            b = boss_by_cn[cn]
            if b.get("dp") is not None:
                dp = b["dp"]

        if cn:
            ws1.append([
                cn, vc, dp, price, disc, club, stock, nm_id, title, img,
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

    if known_updated:
        from sellerops.persistence.mappings import save_vc_known
        save_vc_known(known)

    return n_mapped, n_unmapped

def load_single_shop_rows(sid, shop_name=None):
    """读取某店铺独立映射表 Excel，返回 {vc: row_dict} 与 [unmapped_vc]"""
    p = config.shop_mapping_xlsx(sid, shop_name)
    if not os.path.exists(p):
        return {}, []
    wb = openpyxl.load_workbook(p, data_only=True)
    mapped = {}
    if "在架映射明细" in wb.sheetnames:
        ws = wb["在架映射明细"]
        headers = [str(c.value or "").strip() for c in ws[1]]
        icol = {h: i for i, h in enumerate(headers)}      # 一律按列名定位，无位置兜底
        if find_col(headers, COL_VC) is None:
            warn_missing("单店表·在架映射明细", p, headers, COL_VC,
                         hint="无法按 vendorCode 建键，本店明细按空处理（请运行 sellerops shops-mapping 重建）")
        if find_col(headers, COL_ACTUAL_PRICE) is None:
            warn_missing("单店表·在架映射明细", p, headers, COL_ACTUAL_PRICE,
                         hint="dp 将为空；merge 会用商品价格表按中文名回填，请运行 sellerops merge 重建")

        def cell(r, name):
            i = icol.get(name)
            return r[i] if i is not None and i < len(r) else None

        for r in ws.iter_rows(min_row=2, values_only=True):
            vc = cell(r, COL_VC)
            if not vc:
                continue
            mapped[str(vc)] = {
                "cn": cell(r, COL_CN) or "",
                "vc": str(vc),
                "dp": cell(r, COL_ACTUAL_PRICE),
                "price": cell(r, "本店售价(CNY)"),
                "discount": cell(r, "折扣%"),
                "club": cell(r, "club折扣%"),
                "stock": cell(r, "本店库存"),
                "nmId": cell(r, "本店nmId"),
                "title": cell(r, COL_TITLE) or "",
                "img": cell(r, COL_IMG) or "",
                "L": cell(r, COL_L),
                "W": cell(r, COL_W),
                "H": cell(r, COL_H),
                "weight": cell(r, COL_WT),
                "createAt": cell(r, "创建时间"),
                "updateAt": cell(r, "更新时间"),
                "match_type": cell(r, "匹配类型") or "",
            }
    unmapped = []
    if "未映射在架商品" in wb.sheetnames:
        ws2 = wb["未映射在架商品"]
        for r in ws2.iter_rows(min_row=2, values_only=True):
            if r[0]:
                unmapped.append(str(r[0]))
    wb.close()
    return mapped, unmapped

def list_active_shop_mappings():
    """扫描 data/shops/ 下所有活跃店铺映射表（排除 _archive/ 目录与临时文件）。
    返回 [{'id': sid, 'name': name, 'path': filepath}, ...]"""
    os.makedirs(config.SHOPS_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(config.SHOPS_DIR, "shop_*.xlsx")))
    active = []
    for f in files:
        base = os.path.basename(f)
        if base.startswith("~$") or "_archive" in f:
            continue
        m = re.match(r"^shop_(\d+)(?:_(.+))?\.xlsx$", base)
        if m:
            sid = int(m.group(1))
            name = m.group(2) or f"shop{sid}"
            active.append({"id": sid, "name": name, "path": f})
    return active

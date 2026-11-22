"""多店铺商品审核、归属纠偏与本地映射表合并。"""
from pathlib import Path
import re

from sellerops.settings import paths as config
from sellerops.core.files import safe_load_json
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository
from sellerops.features.catalog import mapping, products, single_shop, workbench

VC_PREFIX_RE = re.compile(config.VC_PREFIX_RE)


def load_known_vcs():
    return mapping.known_vcs_from_mapping()


def calc_new_vcs():
    """所有店铺在架 vc − 已知 − 空商品 = 待审核和前缀建议。
    返回 (normal_items, auto_items, shops_meta)"""
    shops_data, shops_meta = products.load_all_shops()
    known = load_known_vcs()
    prefix_map = MappingRepository.load_prefix_map()

    vc_info = {}
    for sid, rows in shops_data.items():
        for r in rows:
            vc = r.get("vendorCode")
            if not vc or r.get("trashedAt") or products.is_empty_product(r):
                continue
            p = mapping.price_of(r)
            if vc not in vc_info:
                vc_info[vc] = {"title": r.get("title") or "", "per_shop": {}, "shops": [], "img": r.get("repImg") or ""}
            vc_info[vc]["per_shop"][sid] = {"price": p, "stock": mapping.stock_summary(r)}
            if sid not in vc_info[vc]["shops"]:
                vc_info[vc]["shops"].append(sid)

    normal, auto = [], []
    for vc, info in sorted(vc_info.items()):
        if vc in known:
            continue
        prices = [d["price"] for d in info["per_shop"].values() if d["price"] is not None]
        item = {"vc": vc, "title": info["title"], "price": prices[0] if prices else None,
                "shops": info["shops"], "img": info["img"]}
        m = VC_PREFIX_RE.match(vc or "")
        if m:
            pfx = m.group(1)
            boss = prefix_map.get(pfx)
            if boss:
                item.update({"prefix": pfx, "bossSku": boss["sku"],
                             "bossCn": boss["cn"], "bossDp": boss["dp"]})
                auto.append(item)
                continue
        normal.append(item)
    return normal, auto, shops_meta


def gen_review_html(normal_items, shops_meta):
    boss = MappingRepository.load_price_book()
    boss_opts = workbench._boss_opts_html(boss)
    cards = [workbench._review_card_html(i, x, boss_opts) for i, x in enumerate(normal_items, 1)]

    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>多店铺待审核（其余4店新商品）</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f0f2f5;color:#2c3e50}
.bar{position:sticky;top:0;background:#fff;padding:10px 16px;border-bottom:2px solid #8e44ad;z-index:9;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.bar b{color:#8e44ad}button{padding:6px 16px;cursor:pointer;border-radius:6px;border:1px solid #8e44ad;background:#8e44ad;color:#fff}
button.ghost{background:#fff;color:#8e44ad}
.wrap{padding:16px}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;margin:10px 0;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.card.excluded{opacity:.5;background:#f8f8f8}
.card .hd{display:flex;gap:10px;align-items:center;flex:1;min-width:0}
.card .hd .pimg{width:64px;height:64px;object-fit:contain;border-radius:6px;border:1px solid #eee;background:#fafafa}
.card .info{min-width:0}
.card .tt{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:720px}
.card .vc{color:#7f8c8d;font-size:12px;margin-top:2px}
.card .shop{display:inline-block;background:#eee;border-radius:4px;padding:0 5px;margin-left:4px;font-size:11px}
.card .op select{min-width:300px;padding:6px;border:1px solid #ccc;border-radius:4px}
#out{width:100%;height:140px;font-family:Consolas,monospace;margin-top:8px}
.stats{color:#7f8c8d;font-size:13px}
</style></head>
<body>
<div class="bar">
  <b>多店铺待审核工作台</b>
  <span class="stats">新商品 <b id="total"></b> 个 · 已归属 <b id="mapped"></b> · 已排除 <b id="excl"></b> · 待定 <b id="pend"></b></span>
  <button onclick="exportJson()">导出核对结果 JSON</button>
  <button class="ghost" onclick="resetAll()">重置</button>
</div>
<div class="wrap">
<div class="hint" style="color:#2c3e50">这些是<b>其余店铺在架但映射表中没有</b>的 vendorCode（出现在店已标注）。请为每个选择：归属到某个商品价格表商品（补录）或 非货盘商品（排除）。导出后用 catalog merge 保存归属。</div>
CARDS_PLACEHOLDER
<hr><h3>导出结果：</h3><textarea id="out" placeholder="点上方按钮导出"></textarea>
</div>
<script>
const cards = [...document.querySelectorAll('.card')];
document.getElementById('total').textContent = cards.length;
function refresh(){
  let m=0, e=0;
  cards.forEach(cd => {
    const v = cd.querySelector('select').value;
    const isEx = v.startsWith('EXCLUDE');
    cd.classList.toggle('excluded', isEx);
    if (isEx) e++; else if (v) m++;
  });
  document.getElementById('mapped').textContent = m;
  document.getElementById('excl').textContent = e;
  document.getElementById('pend').textContent = cards.length - m - e;
}
function exportJson(){
  const out = [];
  cards.forEach(cd => {
    const v = cd.querySelector('select').value;
    const base = {vc: cd.dataset.vc, title: cd.dataset.title, price: cd.dataset.price, shops: cd.dataset.shops ? cd.dataset.shops.split(',') : []};
    if (!v) { out.push({...base, action: 'pending'}); return; }
    if (v.startsWith('EXCLUDE')) { out.push({...base, action: 'exclude'}); return; }
    const [sku, cn, dp] = v.split('|');
    out.push({...base, action: 'mapped', bossSku: sku, bossCn: cn, bossDp: dp});
  });
  const text = JSON.stringify(out, null, 2);
  document.getElementById('out').value = text;
  downloadJson(text, '多店铺审核.json');
}
function downloadJson(text, filename){
  const blob = new Blob([text], {type: 'application/json;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  alert('已下载 ' + filename + '（同时显示在上方文本框）');
}
function resetAll(){ cards.forEach(cd => { cd.querySelector('select').value=''; }); refresh(); }
refresh();
</script></body></html>'''
    html = html.replace("CARDS_PLACEHOLDER", "\n".join(cards))
    workbench._write(config.current.out_review_html, html)
    print(f"待审核工作台已生成：{config.current.out_review_html}（{len(normal_items)} 个新商品）")


def run_review():
    normal, auto, shops_meta = calc_new_vcs()
    print(f"全部店铺新商品: 普通 {len(normal)} 个 · 前缀待确认 {len(auto)} 个")
    for x in auto:
        print(f'  [自动] {x["vc"]} → {x["bossCn"]}（前缀 {x["prefix"]}，店 {x["shops"]}）')
    gen_review_html(normal + auto, shops_meta)

def _apply_review(rows: list[dict], *, reason: str = '', rename: bool = False) -> int:
    if not isinstance(rows, list):
        raise ValueError('审核文件应包含商品列表')
    products_by_sku = {item['sku']: item for item in MappingRepository.load_price_book(fresh=True)}
    updates = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('vc'), str) or not row['vc'].strip():
            raise ValueError('审核记录缺少完整 VC')
        vc = row['vc'].strip()
        action = 'mapped' if rename else row.get('action')
        if action == 'pending':
            continue
        if action == 'exclude':
            update = ('excluded', None, row.get('reason') or '非货盘商品（人工确认排除）')
        elif action == 'mapped':
            sku = str(row.get('bossSku') or row.get('sku') or '').strip()
            name = str(row.get('bossCn') or row.get('cn') or '').strip()
            matches = [product for product in products_by_sku.values()
                       if (sku and product['sku'] == sku and (not name or product['cn'] == name))
                       or (not sku and name and product['cn'] == name)]
            if len(matches) != 1:
                raise ValueError(f'{vc} 在商品价格表中找不到唯一卖家 SKU')
            update = ('mapped', matches[0]['sku'], row.get('reason') or reason)
        else:
            raise ValueError(f'{vc} 的审核动作无效')
        if vc in updates and updates[vc] != update:
            raise ValueError(f'{vc} 在同批审核记录有冲突')
        updates[vc] = update
    if updates:
        with db.connect(write=True) as connection:
            for vc, (status, sku, note) in updates.items():
                db.set_vc(connection, vc, status, sku=sku, source='manual', note=note)
    return len(updates)


def merge(review_file: str | None = None) -> None:
    count = _apply_review(safe_load_json(review_file)) if review_file else 0
    print(f'已保存 {count} 条审核归属。需要 Excel 时运行 sellerops report mapping。')


def import_mapping(import_file: str) -> None:
    document = safe_load_json(import_file)
    rows = document.get('rows') if isinstance(document, dict) else document
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('核对文件应包含旧版工作台的商品列表')
    reviews = []
    for row in rows:
        codes = row.get('vendorCodes', [])
        if 'action' in row or not isinstance(codes, list):
            raise ValueError('旧格式需包含 vendorCodes；多店审核结果请用 catalog merge')
        reviews.extend({'vc': vc, 'action': 'mapped', 'bossSku': row.get('sku'),
                        'bossCn': row.get('cn')} for vc in codes)
    print(f'已导入 {_apply_review(reviews)} 条归属')


def set_vc_override(vc: str = '', new_cn: str = '', reason: str = '人工纠偏',
                    file_path: str = '') -> bool:
    rows = safe_load_json(file_path) if file_path else [{'vc': vc, 'cn': new_cn}]
    return _apply_review(rows, reason=reason, rename=True) > 0


def _export_destination(name: str, output: str = '') -> Path:
    destination = Path(output).resolve() if output else config.current.exports_dir / name
    if (destination.suffix.lower() != '.xlsx' or destination.name == '商品价格表.xlsx'
            or Path(config.current.data_dir) in destination.parents
            or (config.PROJECT_ROOT / 'data') in destination.parents):
        raise ValueError('请选择数据目录外的 .xlsx 报表路径')
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def export_mapping(shop_id: int | None = None, output: str = '') -> Path:
    if shop_id is not None:
        path = _export_destination(f'shop_{shop_id}.xlsx', output)
        single_shop.build_single_shop_xlsx(shop_id, path)
    else:
        from sellerops.features.catalog.mapping_excel import build_xlsx

        state, excluded = MappingRepository.load_mapping_state()
        shops_data, shops = products.load_all_shops()
        main_id = mapping.main_shop(config.current)
        coverage = {}
        for sid, rows in shops_data.items():
            for raw in rows:
                vc = raw.get('vendorCode')
                if vc:
                    coverage.setdefault(vc, {})[sid] = {
                        'price': mapping.price_of(raw), 'stock': mapping.stock_summary(raw),
                        'nmId': raw.get('nmId'), 'createAt': raw.get('createAt'), 'updateAt': raw.get('updateAt'),
                        'title': raw.get('title'), 'repImg': raw.get('repImg'),
                        **{key: raw.get(key) for key in ('discount', 'clubDiscount', 'dimensionsLength',
                           'dimensionsWidth', 'dimensionsHeight', 'dimensionsWeightBrutto')},
                    }
        records = [{'sku': row['sku'], 'cn': row['cn'], 'dp': row['dp'], 'vendorCodes': [vc]}
                   for vc, row in state.items() if vc in coverage]
        path = _export_destination('价格映射表.xlsx', output)
        build_xlsx([], records, mapping.priced_rows(shops_data.get(main_id, [])),
                   excluded_vcs=excluded, shop_coverage=coverage, shops_meta=shops, main_sid=main_id,
                   out_path=path)
    print(f'已导出：{path}')
    return path


def sync_all_shops_mapping(shop_id: int | None = None) -> None:
    if shop_id is not None:
        export_mapping(shop_id)
        return
    for shop in products.snapshot_shops():
        export_mapping(shop['id'])


def run_export(args) -> int:
    export_mapping(args.shop_id, args.out)
    return 0

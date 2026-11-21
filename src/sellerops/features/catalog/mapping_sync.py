"""多店铺商品审核、归属纠偏与本地映射表合并。"""
from datetime import datetime
import json
import os
from pathlib import Path
import re

from sellerops.settings import paths as config
from sellerops.core.files import safe_load_json
from sellerops.domain.bcs_products import deduplicate_bcs_products
from sellerops.persistence.mapping_batch import mapping_batch
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.features.catalog import mapping, products, single_shop, workbench

VC_PREFIX_RE = re.compile(config.VC_PREFIX_RE)


def load_known_vcs():
    """已知 vendorCode = 全局归属池 + 纠偏 + 排除 + 映射表"""
    known_set = set()
    kn, ov, ex = mapping.load_vc_registry()
    known_set.update(kn.keys())
    known_set.update(ov.keys())
    known_set.update(ex.keys())
    if os.path.exists(config.current.mapping_xlsx):
        st, excl = MappingRepository.load_mapping_state()
        known_set.update(st.keys())
        known_set.update(excl.keys())
    return known_set


def calc_new_vcs():
    """所有店铺在架 vc − 已知 − 空商品 = 待审核/自动补录。
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
<div class="hint" style="color:#2c3e50">这些是<b>其余店铺在架但映射表中没有</b>的 vendorCode（出现在店已标注）。请为每个选择：归属到某个商品价格表商品（补录）或 非货盘商品（排除）。导出后发我合并进映射表。</div>
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
    print(f"全部店铺新商品: 普通 {len(normal)} 个 · 前缀自动补录 {len(auto)} 个")
    for x in auto:
        print(f'  [自动] {x["vc"]} → {x["bossCn"]}（前缀 {x["prefix"]}，店 {x["shops"]}）')
    gen_review_html(normal, shops_meta)


def _target_shops(shop_id):
    tables = {s["id"]: s for s in single_shop.list_active_shop_mappings()}
    shops = tables.copy()
    for shop in products.snapshot_shops():
        sid = int(shop["id"])
        existing = tables.get(sid, {})
        name = shop.get("name") or existing.get("name") or f"shop{sid}"
        path = config.shop_mapping_xlsx(sid, name) if shop.get("name") or not existing else existing["path"]
        shops[sid] = {"id": sid, "name": name, "path": path}
    if shop_id is not None and config.is_shop_archived(shop_id):
        print(f"店铺 {shop_id} 已归档，跳过刷新。")
        return []
    if shop_id is not None and shop_id not in shops:
        raise ValueError(f"找不到目标店铺 {shop_id}，未更新映射")
    candidates = [shops[shop_id]] if shop_id is not None else list(shops.values())
    active = [s for s in candidates if not config.is_shop_archived(s["id"])]
    if not active:
        raise ValueError("没有活跃店铺可生成映射，未更新文件")
    return active


def _snapshot_rows(shop):
    sid = shop["id"]
    path = config.shop_json_path(sid)
    try:
        document = ProductSnapshotRepository.load_snapshot_document(sid)
        if document is None:
            raise ValueError("缺少商品快照")
        return document["rows"]
    except Exception as exc:
        raise ValueError(f"店铺 {sid} 的快照 {path}：{exc}。本次未更新映射。") from exc


def _apply_review(rows, known, excluded, boss):
    if not isinstance(rows, list):
        raise ValueError("审核文件应为审核工作台导出的商品列表")
    prices = {b["sku"]: b["dp"] for b in boss if b.get("sku")}
    for row in rows:
        if not isinstance(row, dict) or not row.get("vc"):
            raise ValueError("审核记录缺少商品编码 vc")
        vc = row["vc"]
        if row.get("action") == "pending":
            continue
        if row.get("action") == "exclude":
            excluded[vc] = "非货盘商品（人工确认排除）"
        elif row.get("action") == "mapped" and row.get("bossCn"):
            sku = row.get("bossSku") or ""
            price = prices.get(sku)
            known[vc] = {"cn": row["bossCn"], "sku": sku, "source": "审核补录",
                         "dp": row.get("bossDp") if price is None else price}
        else:
            raise ValueError(f"商品 {vc} 的审核动作或中文名无效")


def _apply_renames(rows, reason, known, overrides, boss):
    if not isinstance(rows, list) or not rows:
        raise ValueError("请提供要改名的商品编码和中文名")
    by_name = {b["cn"]: b for b in boss}
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("改名记录应包含 vc 和 cn")
        vc, cn = (str(row.get(k) or "").strip() for k in ("vc", "cn"))
        if not vc or not cn:
            raise ValueError("改名记录的商品编码和中文名不能为空")
        overrides[vc] = {"cn": cn, "reason": row.get("reason") or reason, "updatedAt": stamp}
        if vc not in known:
            product = by_name.get(cn, {})
            known[vc] = {"sku": product.get("sku", ""), "dp": product.get("dp"),
                         "source": "人工纠偏", "cnUpdatedAt": stamp}
        known[vc]["cn"] = cn


def _write_total(batch, shops, mapped_rows, snapshots, registry, boss):
    known, _, excluded = registry
    boss_by_name = {b["cn"]: b for b in boss}
    main_id = mapping.main_shop(config.current)
    coverage = {}
    for sid, rows in mapped_rows.items():
        for vc, row in rows.items():
            coverage.setdefault(vc, {})[sid] = row

    entries = []
    for vc, stores in sorted(coverage.items()):
        row = stores[main_id] if main_id in stores else stores[min(stores)]
        history = known.get(vc, {})
        cn = row.get("cn") or history.get("cn") or ""
        product = boss_by_name.get(cn, {})
        price = product.get("dp")
        entries.append({"sku": product.get("sku", history.get("sku", "")), "cn": cn,
                        "dp": row.get("dp") if price is None else price, "vendorCodes": [vc],
                        "note": row.get("match_type") or "店铺映射汇聚"})

    if main_id in snapshots:
        main_rows = deduplicate_bcs_products(snapshots[main_id], main_id)
        bcs_rows = mapping.priced_rows(main_rows)
    else:
        bcs_rows = [] if snapshots else mapping.load_bcs()
    mapping.build_xlsx([], entries, bcs_rows, excluded_vcs=excluded, shop_coverage=coverage,
                       shops_meta=shops, main_sid=main_id, load_boss_fn=lambda: boss,
                       out_path=batch.stage(config.current.mapping_xlsx))


def _refresh(command, *, shop_id=None, input_file=None, vc=None, new_cn=None,
             reason=None, sync_shops=True):
    with mapping_batch(command, input_file=input_file, shop_id=shop_id, vc=vc, cn=new_cn,
                       reason=reason, sync_shops=sync_shops) as batch:
        if batch is None:
            return
        shops = _target_shops(shop_id)
        if not shops:
            return
        snapshots = {s["id"]: _snapshot_rows(s) for s in shops} if sync_shops else {}
        if not Path(config.current.price_book_xlsx).is_file():
            raise FileNotFoundError(f"缺少商品价格表：{config.current.price_book_xlsx}")
        boss = MappingRepository.load_price_book()
        prefix_map = MappingRepository.load_prefix_map(strict=True)
        registry = mapping.load_vc_registry()
        known, overrides, excluded = registry
        if command == "rename":
            rows = batch.input_data if input_file else [{"vc": vc, "cn": new_cn}]
            _apply_renames(rows, reason, known, overrides, boss)
        elif input_file:
            _apply_review(batch.input_data, known, excluded, boss)

        mapped_rows = {}
        for shop in shops:
            sid, name, path = shop["id"], shop["name"], shop["path"]
            if sync_shops:
                path = batch.stage(path)
                try:
                    counts = single_shop.build_single_shop_xlsx(
                        sid, name, rows=snapshots[sid], registry=registry, boss=boss,
                        prefix_map=prefix_map, out_path=path)
                except ValueError as exc:
                    raise ValueError(f"店铺 {sid} 的快照 {config.shop_json_path(sid)}：{exc}。本次未更新映射。") from exc
                print(f"已准备店铺 {sid}（{name}）：已映射 {counts[0]}，未映射 {counts[1]}")
            if command != "shop-mappings":
                if not Path(path).is_file():
                    raise FileNotFoundError(f"缺少店铺 {sid} 的映射表：{path}")
                mapped_rows[sid], _ = single_shop.load_single_shop_rows(sid, name, path=path)

        data = config.current
        for path, values in zip((data.vc_known_json, data.vc_override_json, data.vc_excluded_json), registry):
            previous = safe_load_json(path, default=None if path == data.vc_known_json else {})
            if values != previous:
                batch.stage(path).write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
        if command != "shop-mappings":
            _write_total(batch, shops, mapped_rows, snapshots, registry, boss)
    output = data.shops_dir if command == "shop-mappings" else data.mapping_xlsx
    print(f"映射已保存：共 {len(shops)} 个店铺。文件位置：{output}")


def merge(review_file=None, sync_shops=True):
    _refresh("merge", input_file=review_file, sync_shops=sync_shops)


def set_vc_override(vc=None, new_cn=None, reason="人工纠偏", file_path=None):
    _refresh("rename", input_file=file_path, vc=vc, new_cn=new_cn, reason=reason)
    return True


def sync_all_shops_mapping(shop_id=None, force=False):
    _refresh("shop-mappings", shop_id=shop_id)

# -*- coding: utf-8 -*-
"""
wb_ops 多店铺同步 / 待审核 / 增量合并（原 sync_shops.py）

review：找出其他店铺在架但映射表中没有的 vc（前缀命中→自动补录，其余→待审核）。
merge：增量合并（映射表 xlsx = 唯一状态源，继承旧归属 + 追加审核 + 消失即移除）。
"""
import json
import os
import re

from . import config
from . import mapping
from . import products
from . import workbench
VC_PREFIX_RE = re.compile(config.VC_PREFIX_RE)


def load_known_vcs():
    """已知 vendorCode = 全局归属池 + 纠偏 + 排除 + 映射表"""
    known_set = set()
    kn, ov, ex = mapping.load_vc_registry()
    known_set.update(kn.keys())
    known_set.update(ov.keys())
    known_set.update(ex.keys())
    if os.path.exists(config.MAPPING_XLSX):
        st, excl = mapping.load_mapping_state()
        known_set.update(st.keys())
        known_set.update(excl.keys())
    return known_set


def calc_new_vcs():
    """所有店铺在架 vc − 已知 − 空商品 = 待审核/自动补录。
    返回 (normal_items, auto_items, shops_meta)"""
    shops_data, shops_meta = products.load_all_shops()
    known = load_known_vcs()
    prefix_map = mapping.load_prefix_map()

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
    boss = mapping.load_boss()
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
    workbench._write(config.OUT_REVIEW_HTML, html)
    print(f"待审核工作台已生成：{config.OUT_REVIEW_HTML}（{len(normal_items)} 个新商品）")


def build_shop_coverage():
    """从全部店铺 JSON 或单店表构建 shop_coverage: {vc: {sid: {price, stock, title, img}}}"""
    shops_data, shops_meta = products.load_all_shops()
    cov = {}
    for sid, rows in shops_data.items():
        for r in rows:
            vc = r.get("vendorCode")
            if not vc or r.get("trashedAt"):
                continue
            cov.setdefault(vc, {})[sid] = {"price": mapping.price_of(r), "stock": mapping.stock_summary(r),
                                           "title": r.get("title") or "", "img": r.get("repImg") or "",
                                           "nmId": r.get("nmId"), "createAt": r.get("createAt"),
                                           "updateAt": r.get("updateAt")}
    return cov, shops_meta


def sync_all_shops_mapping(shop_id=None, force=False):
    """刷新或生成指定店铺（或全部活跃店铺）的独立映射表（data/shops/shop_*.xlsx）。"""
    try:
        shops_data, shops_meta = products.load_all_shops()
    except Exception as e:
        print(f"[提示] 未能从快照加载全部店铺：{e}")
        shops_data, shops_meta = {}, []

    known, overrides, excluded = mapping.load_vc_registry()
    boss = mapping.load_boss()
    prefix_map = mapping.load_prefix_map()

    target_shops = [s for s in shops_meta if (shop_id is None or s["id"] == shop_id)]
    if not target_shops:
        print(f"[提示] 未找到店铺 {shop_id} 的快照数据，请确认是否已运行 wb.py fetch")
        return

    print(f"\n>>> 正在同步 {len(target_shops)} 个店铺的独立映射表（data/shops/）...")
    for s in target_shops:
        sid = s["id"]
        sname = s.get("name") or f"shop{sid}"
        if config.is_shop_archived(sid):
            print(f"  [跳过] 店铺 {sid} ({sname}) 已在 _archive/ 归档，跳过单店同步")
            continue
        rows = shops_data.get(sid, [])
        m, u = mapping.build_single_shop_xlsx(sid, sname, rows=rows,
                                              registry=(known, overrides, excluded),
                                              boss=boss, prefix_map=prefix_map)
        print(f"  店 {sid} ({sname})：在架归属 {m} 个 · 未映射 {u} 个 → {os.path.basename(config.shop_mapping_xlsx(sid, sname))}")


def set_vc_override(vc=None, new_cn=None, reason="人工纠偏", file_path=None):
    """设置 VC 中文名纠偏改名，并自动级联更新所有单店表与总表。"""
    from datetime import datetime
    known, overrides, excluded = mapping.load_vc_registry()
    updated = []
    if file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            items = json.load(f)
            for it in items:
                v = it.get("vc")
                c = it.get("cn")
                r = it.get("reason") or reason
                if v and c:
                    overrides[v] = {"cn": c, "reason": r, "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    if v in known:
                        known[v]["cn"] = c
                    updated.append(v)
    elif vc and new_cn:
        overrides[vc] = {"cn": new_cn, "reason": reason, "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if vc in known:
            known[vc]["cn"] = new_cn
        updated.append(vc)
    else:
        print("[错误] 未指定 --vc 和 --cn，或未指定有效 --file")
        return False

    mapping.save_vc_registry(known=known, overrides=overrides)
    print(f"[纠偏登记] 已将 {len(updated)} 个 VC 改名写入 {config.VC_OVERRIDE_JSON}")
    for v in updated:
        print(f"  {v} → {overrides[v]['cn']} ({overrides[v]['reason']})")

    # 重新执行 merge，自动级联更新所有包含该 VC 的单店表与总表
    print("\n>>> 开始级联刷新各单店表与聚合总表...")
    merge(None)
    print("\n[成功] 全店铺单表与聚合总表现已全部同步为最新名称！")
    return True


def merge(review_file=None, sync_shops=True):
    """多店铺增量合并映射表：
    1. 审核文件录入：若有 review_file，更新全局已知/排除池；
    2. 同步单店映射表：若有快照，更新 data/shops/ 下各单店表；
    3. 汇聚活跃单店表：扫描 data/shops/ 活跃单表（排除 _archive/），做 Outer Join；
    4. 重建全景总表：生成 8-Sheet data/价格映射表.xlsx，完全向下兼容。
    """
    known, overrides, excluded = mapping.load_vc_registry()
    boss = mapping.load_boss()
    dp_by_sku = {b["sku"]: b["dp"] for b in boss if b.get("sku")}
    boss_by_cn = {b["cn"]: b for b in boss if b.get("cn")}
    prefix_map = mapping.load_prefix_map()

    # 1. 处理审核文件
    um = []
    if review_file and os.path.exists(review_file):
        with open(review_file, "r", encoding="utf-8") as f:
            um = json.load(f)
        n_map, n_excl = 0, 0
        for x in um:
            act = x.get("action")
            vc = x.get("vc")
            if not vc:
                continue
            if act == "exclude":
                excluded[vc] = "非货盘商品（人工确认排除）"
                n_excl += 1
            elif act == "mapped":
                sku = x.get("bossSku") or ""
                dp = dp_by_sku.get(sku) or x.get("bossDp")
                cn = x.get("bossCn") or ""
                known[vc] = {"cn": cn, "sku": sku, "dp": dp, "source": "审核补录"}
                n_map += 1
        mapping.save_vc_registry(known=known, excluded=excluded)
        print(f"[审核补录] 已将 {n_map} 条归属、{n_excl} 条排除同步至全局归属池")
    elif review_file:
        print(f"[警告] 审核文件不存在：{review_file}，按无新审核执行")

    # 2. 同步刷新各店铺独立映射表
    try:
        shops_data, shops_meta = products.load_all_shops()
    except Exception as e:
        shops_data, shops_meta = {}, []

    if sync_shops and shops_meta:
        print(f"\n>>> 正在同步各店铺独立映射表（data/shops/）...")
        for s in shops_meta:
            sid = s["id"]
            sname = s.get("name") or f"shop{sid}"
            if config.is_shop_archived(sid):
                continue
            rows = shops_data.get(sid, [])
            if rows:
                n_m, n_u = mapping.build_single_shop_xlsx(
                    sid, sname, rows=rows,
                    registry=(known, overrides, excluded),
                    boss=boss, prefix_map=prefix_map
                )
                print(f"  [单店同步] 店铺 {sid} ({sname})：已映射 {n_m} · 未映射 {n_u} → {os.path.basename(config.shop_mapping_xlsx(sid, sname))}")

    # 3. 扫描 data/shops/ 下活跃单店映射表（排除 _archive/ 目录）
    active_shops = mapping.list_active_shop_mappings()
    if not active_shops:
        if shops_meta:
            sync_all_shops_mapping()
            active_shops = mapping.list_active_shop_mappings()

    if not active_shops:
        print("[错误] 未能找到任何活跃单店映射表，请先运行 wb.py fetch 并在 data/shops/ 确认文件")
        return

    all_shop_rows = {}
    shop_coverage = {}
    for s in active_shops:
        sid = s["id"]
        sname = s["name"]
        mapped, unmapped = mapping.load_single_shop_rows(sid, sname)
        all_shop_rows[sid] = mapped
        for vc, r in mapped.items():
            shop_coverage.setdefault(vc, {})[sid] = {
                "price": r.get("price"),
                "stock": r.get("stock") or "",
                "title": r.get("title") or "",
                "img": r.get("img") or "",
                "nmId": r.get("nmId"),
                "createAt": r.get("createAt"),
                "updateAt": r.get("updateAt"),
            }

    # 4. 构建 Outer Join 活跃在架 VC 并集
    alive_vcs = sorted(shop_coverage.keys())
    sid_main = mapping.shop_id()
    active_shops_meta = [{"id": s["id"], "name": s["name"]} for s in active_shops]

    extra = []
    for vc in alive_vcs:
        cov_shops = shop_coverage[vc]
        rep_sid = sid_main if sid_main in cov_shops else sorted(cov_shops.keys())[0]
        rep_row = all_shop_rows[rep_sid].get(vc, {})
        cn = rep_row.get("cn") or (known.get(vc, {}).get("cn") if vc in known else "")
        dp = rep_row.get("dp")
        b = boss_by_cn.get(cn)
        if b and b.get("dp") is not None:
            dp = b["dp"]
        sku = b["sku"] if b else (known.get(vc, {}).get("sku") or "")

        extra.append({
            "sku": sku,
            "cn": cn,
            "dp": dp,
            "vendorCodes": [vc],
            "note": rep_row.get("match_type") or "店铺映射汇聚",
        })

    # 5. 店铺全量商品（Sheet5）
    bcs_rows = []
    try:
        bcs_rows = mapping.load_bcs()
    except Exception:
        if active_shops:
            first_sid = active_shops[0]["id"]
            p_json = config.shop_json_path(first_sid)
            if os.path.exists(p_json):
                d = json.load(open(p_json, encoding="utf-8"))
                rows = [r for r in d.get("rows", []) if not r.get("trashedAt")]
                for r in rows:
                    sl = r.get("sizeList") or []
                    p = sl[0].get("price") if sl else None
                    if p is not None:
                        c = dict(r)
                        c["price"] = int(p)
                        c["vc"] = r.get("vendorCode") or ""
                        c["wbnm"] = str(r.get("nmId")) if r.get("nmId") else ""
                        c["img"] = r.get("repImg") or ""
                        bcs_rows.append(c)

    # 6. 生成全景 8-Sheet 价格映射表.xlsx
    n_boss, n_pick, n_unmap, n_multi = mapping.build_xlsx(
        [], extra, bcs_rows, excluded_vcs=excluded,
        shop_coverage=shop_coverage, shops_meta=active_shops_meta
    )

    print(f"\n[合并完成] 聚合总表已生成：{config.MAPPING_XLSX}")
    print(f"  活跃店铺：{len(active_shops)} 个 ({', '.join(s['name'] for s in active_shops)})")
    print(f"  映射商品：在架归属 {n_pick} 个 · 货盘匹配 {n_boss} · 多重映射 {n_multi} · 已排除 {len(excluded)}")


# ---------------- 入口逻辑（供 cli 调用） ----------------
def run_review():
    normal, auto, shops_meta = calc_new_vcs()
    print(f"全部店铺新商品: 普通 {len(normal)} 个 · 前缀自动补录 {len(auto)} 个")
    for x in auto:
        print(f'  [自动] {x["vc"]} → {x["bossCn"]}（前缀 {x["prefix"]}，店 {x["shops"]}）')
    gen_review_html(normal, shops_meta)


def run_merge(review_file=None):
    merge(review_file)


def print_write_hint():
    """写操作未加 --sync 时打印的提示：告知用户当前未做写后验证/同步/合并，以及如何补做。"""
    print("\n[提示] 本次未做写后验证。因 WB/BCS 存在异步回填与延迟，当场验证不一定准确。")
    print("       如需同步在架商品并合并到映射表，请运行：python wb.py fetch && python wb.py merge")
    print("       （或在原写命令后加 --sync，命令内自动完成同步+合并）")


def post_write_merge(fetch=True):
    """写后验证后自动增量合并映射表。fetch=True 时先全店同步拉取（拿最新快照）。
    merge 会做「消失即移除」：下架/清理后对应商品将从映射表移除。失败不中断主命令。"""
    try:
        if fetch:
            products.fetch_all()
        merge(None)
    except Exception as e:
        print(f"[自动merge] 失败：{e}（映射表未更新，可稍后手动 wb.py merge）")

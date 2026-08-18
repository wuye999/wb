# -*- coding: utf-8 -*-
"""
wb_ops HTML 工作台渲染（合并原 build_mapping / sync_shops / gen_unmapped_check 三处重复模板）

职责：把「核对工作台」的 HTML/CSS/JS 统一集中到这里，去重。
只做渲染，不碰数据；数据由 mapping.py / mapping_sync.py 提供。
"""
import json
from html import escape

from . import config
from . import keywords
# ---------------- 卡片 / 分组片段 ----------------
def _cand_html(c):
    """候选单元格（图/俄文名/vc/价格/命中数）"""
    img = escape(c["img"])
    return (f'<div class="cand" data-vc="{escape(c["vc"])}" data-title="{escape(c["title"])}" '
            f'data-price="{c["price"]}">'
            f'<img src="{img}" loading="lazy" onerror="this.style.opacity=.12">'
            f'<div class="ci"><div class="tt">{escape(c["title"][:80])}</div>'
            f'<div class="vc">{escape(c["vc"])} · ¥{c["price"]}</div>'
            f'<div class="hit">关键词命中 {c["hit"]}</div></div></div>')


def _conflict_group_html(g):
    """同价冲突组（多商品价格表商品共享候选池 + 归属列勾选）"""
    bosses = g["bosses"]
    head = " / ".join(f'<b>{escape(b["cn"])}</b>(¥{b["dp"]})' for b in bosses)
    cands = sorted(g["cands"], key=lambda c: -c["hit"])
    rows = []
    for c in cands:
        checkboxes = "".join(
            f'<td class="own"><label><input type="checkbox" data-boss="{b["idx"]}" data-vc="{escape(c["vc"])}" class="gpick">'
            f'<span>{b["idx"]}</span></label></td>'
            for b in bosses)
        cls = "missrow" if c["hit"] == 0 else ""
        rows.append(f'<tr class="{cls}">{checkboxes}<td class="candtd">{_cand_html(c)}</td></tr>')
    miss_n = sum(1 for c in cands if c["hit"] == 0)
    return f'''
<div class="grp conflict" id="boss-{bosses[0]["idx"]}">
  <div class="ghead">⚠ 同价冲突组（floor={g["floor"]}）：{head}
    <span class="cnt">{len(cands)} 个候选（其中 {miss_n} 个无关键词命中）</span></div>
  <div class="hint">候选池共享：在候选行勾选归属到对应商品价格表商品（多选=同款多变体；无关键词命中的候选默认半透明，可点顶部按钮切换显示）</div>
  <table class="gtable">
    <thead><tr>{"".join(f'<th>{b["idx"]}. {escape(b["cn"])}<br>¥{b["dp"]}</th>' for b in bosses)}<th>店铺候选（图/俄文名/vendorCode/价格）</th></tr></thead>
    {"".join(rows)}
  </table>
</div>'''


def _single_group_html(g):
    """普通商品卡片（候选勾选，命中>0 主区 + 命中=0 折叠）"""
    b = g["bosses"][0]
    cands = sorted(g["cands"], key=lambda c: -c["hit"])
    hit_cands = [c for c in cands if c["hit"] > 0]
    miss_cands = [c for c in cands if c["hit"] == 0]
    pick_html = "".join(
        f'<label class="pickw"><input type="checkbox" class="spick" data-vc="{escape(c["vc"])}" '
        f'data-title="{escape(c["title"])}" data-price="{c["price"]}">{_cand_html(c)}</label>'
        for c in hit_cands)
    miss_html = ""
    if miss_cands:
        miss_html = ('<details class="missbox"><summary>无关键词命中的候选（%d 个，多为不同类商品，谨慎）</summary><div class="misswrap">%s</div></details>'
                     % (len(miss_cands),
                        "".join(f'<label class="pickw"><input type="checkbox" class="spick" data-vc="{escape(c["vc"])}" data-title="{escape(c["title"])}" data-price="{c["price"]}">{_cand_html(c)}</label>'
                                for c in miss_cands)))
    pick_state = ('<span class="ok">候选唯一（关键词命中 1 个）</span>' if len(hit_cands) == 1 else "")
    return f'''
<div class="grp single" id="boss-{b["idx"]}">
  <div class="ghead"><span class="idx">#{b["idx"]}</span> <b>{escape(b["cn"])}</b>
    <span class="sku">{escape(b["sku"])}</span> <span class="dp">双倍售价 ¥{b["dp"]}</span>
    <span class="kw">关键词: {escape(", ".join(keywords.keywords_for(b["cn"])) or "无")}</span>
    <span class="picked"></span> {pick_state}</div>
  {miss_html}
  <div class="pickrow">{pick_html}</div>
</div>'''


def _todo_group_html(b):
    """待核查（无候选手工填 vc）"""
    return f'''
<div class="grp todo" id="boss-{b["idx"]}">
  <div class="ghead">⚠ 待核查 #{b["idx"]} <b>{escape(b["cn"])}</b> <span class="sku">{escape(b["sku"])}</span>
    <span class="dp">双倍售价 ¥{b["dp"]}</span></div>
  <div class="hint">店铺中无价格 {b["floor"]}/{b["floor"] + 1} 元的商品 → 可能未上架/价格特殊/漏配。
     若你已知对应 vendorCode，可在此填写（逗号分隔）：</div>
  <input class="manual" placeholder="BCS-XXXX-123456789, BCS-YYYY-987654321">
</div>'''


def _boss_opts_html(boss):
    """商品价格表商品下拉选项（vc 卡片归属用）"""
    return [f'<option value="{escape(b["sku"])}|{escape(b["cn"])}|{b["dp"]}">'
            f'#{b["idx"]} {escape(b["cn"])} · {escape(b["sku"])} · ¥{b["dp"]}</option>'
            for b in boss]


def _review_card_html(i, x, boss_opts):
    """未归属 vc 卡片（下半区/多店铺待审核共用）：图/名/vc/价格/店铺 + 归属下拉"""
    shops_badge = " ".join(f'<span class="shop">店{s}</span>' for s in x["shops"])
    img = escape(x.get("img") or "")
    return f'''
<div class="card" data-vc="{escape(x["vc"])}" data-title="{escape(x["title"])}" data-price="{x["price"]}"
     data-shops="{",".join(map(str, x["shops"]))}">
  <div class="hd">
    <span class="idx">#{i}</span>
    <img class="pimg" src="{img}" loading="lazy" onerror="this.style.display='none'">
    <div class="info">
      <div class="tt">{escape(x["title"])}</div>
      <div class="vc">{escape(x["vc"])} · ¥{x["price"]} · {shops_badge}</div>
    </div>
  </div>
  <div class="op">
    <select class="assign" onchange="refresh()">
      <option value="" selected>— 待定（默认）—</option>
      <option value="EXCLUDE|非货盘商品|">🚫 非货盘商品（排除，不映射）</option>
      {"".join(boss_opts)}
    </select>
  </div>
</div>'''


# ---------------- 旧版工作台（legacy：仅主店） ----------------
def render_html(conflict_groups, single_groups, todo):
    conflict_html = [_conflict_group_html(g) for g in conflict_groups]
    single_html = [_single_group_html(g) for g in single_groups]
    todo_html = [_todo_group_html(b) for b in todo]

    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>价格映射核对工作台（主店）</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f0f2f5;color:#2c3e50}
.bar{position:sticky;top:0;background:#fff;padding:10px 16px;border-bottom:2px solid #3498db;z-index:9;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.bar b{color:#e74c3c}button{padding:6px 16px;cursor:pointer;border-radius:6px;border:1px solid #3498db;background:#3498db;color:#fff}
button.ghost{background:#fff;color:#3498db}
.wrap{padding:16px}
.grp{background:#fff;border:1px solid #ddd;border-radius:8px;margin:12px 0;padding:10px 14px}
.ghead{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.ghead b{font-size:15px}.idx{color:#7f8c8d;font-weight:bold}.sku{color:#2980b9}.dp{color:#e74c3c;font-weight:bold}
.kw{color:#95a5a6;font-size:12px}.cnt{color:#7f8c8d;font-size:12px}.hint{color:#8e44ad;font-size:12px;margin:4px 0}
.ok{color:#27ae60;font-size:12px}.picked{color:#27ae60;font-weight:bold}
.conflict .ghead{background:#fdf2e9;border-left:4px solid #e67e22;padding:4px 8px;border-radius:4px}
.todo .ghead{background:#fdedec;border-left:4px solid #c0392b;padding:4px 8px;border-radius:4px}
.gtable{border-collapse:collapse;width:100%}.gtable th{background:#f8f9fa;font-size:12px;padding:6px;border:1px solid #ddd}
.gtable td{border:1px solid #eee;vertical-align:top}.gtable .own{text-align:center;width:70px;font-size:12px}
.gtable .own label{display:block;padding:4px;cursor:pointer}
.gtable .own input:checked+span{color:#27ae60;font-weight:bold}
.missrow{opacity:.45}.missrow:hover{opacity:1}
.candtd{width:380px}.cand{display:flex;gap:8px;align-items:center;padding:4px}
.cand img{width:52px;height:52px;object-fit:contain;border-radius:4px;background:#fafafa;border:1px solid #eee}
.cand .ci{font-size:12px;line-height:1.4}.cand .vc{color:#7f8c8d}.cand .hit{color:#27ae60;font-size:11px}
.pickrow{display:flex;flex-wrap:wrap;gap:8px}
.pickw{display:flex;gap:6px;align-items:flex-start;border:1px solid #eee;border-radius:8px;padding:6px;cursor:pointer;width:300px}
.pickw:has(input:checked){outline:2px solid #27ae60;background:#f0fff4}
.pickw img{width:52px;height:52px;object-fit:contain;background:#fafafa;border-radius:4px}
.pickw .cand{flex:1}.misswrap{display:flex;flex-wrap:wrap;gap:8px}
.missbox summary{color:#95a5a6;cursor:pointer;font-size:12px;margin:6px 0}
.manual{width:60%;padding:6px;border:1px solid #ccc;border-radius:4px}
#out{width:100%;height:140px;font-family:Consolas,monospace;margin-top:8px}
</style></head>
<body>
<div class="bar">
  <b>价格映射核对工作台</b> <span>主店 · 商品价格表商品 <b id="total"></b> 个 · 已归属 <b id="picked"></b> 个</span>
  <button onclick="exportJson()">导出核对结果 JSON</button>
  <button class="ghost" onclick="clearAll()">清空</button>
  <button class="ghost" onclick="toggleMiss()">显示/隐藏无关键词命中候选</button>
</div>
<div class="wrap">
<div class="hint" style="color:#2c3e50">操作：每个商品勾选<b>实际对应的店铺商品</b>（看图片/俄文名/价格确认，可多选=同款多变体）。同价冲突组勾选候选归属到对应商品价格表商品。核对完点「导出核对结果 JSON」→ 把内容发给我，我生成映射表 xlsx。</div>
<h3>一、待核查（店铺中找不到对应价格，需人工排查）</h3>
TODO_PLACEHOLDER
<h3>二、同价冲突组（候选池共享，人工图片区分归属）</h3>
CONFLICT_PLACEHOLDER
<h3>三、普通商品（候选通常唯一，勾选确认）</h3>
SINGLE_PLACEHOLDER
<hr><h3>导出结果：</h3><textarea id="out" placeholder="点上方按钮导出"></textarea>
</div>
<script>
const data = DATA_JSON;
document.getElementById('total').textContent = data.bossCount;
function refresh(){
  data.bosses.forEach(b => {
    let c = 0;
    document.querySelectorAll('.spick:checked').forEach(i => {
      const card = i.closest('.grp'); if (card && card.id === 'boss-' + b.idx) c++; });
    document.querySelectorAll('.gpick:checked').forEach(i => { if (+i.dataset.boss === b.idx) c++; });
    const el = document.querySelector('#boss-' + b.idx + ' .picked');
    if (el) el.textContent = c ? '已归属 ' + c + ' 个' : '';
  });
  document.querySelector('#picked').textContent =
    document.querySelectorAll('.spick:checked').length + document.querySelectorAll('.gpick:checked').length;
}
document.querySelectorAll('.spick,.gpick').forEach(i => i.addEventListener('change', refresh));
function exportJson(){
  const byIdx = {};
  data.bosses.forEach(b => byIdx[b.idx] = {idx: b.idx, sku: b.sku, cn: b.cn, dp: b.dp, vcs: []});
  document.querySelectorAll('.spick:checked').forEach(i => {
    const card = i.closest('.grp'); const m = card ? card.id.match(/^boss-(\\d+)$/) : null;
    const b = m ? byIdx[+m[1]] : null;
    if (b && i.dataset.vc && !b.vcs.includes(i.dataset.vc)) b.vcs.push(i.dataset.vc);
  });
  document.querySelectorAll('.gpick:checked').forEach(i => {
    const b = byIdx[+i.dataset.boss];
    if (b && i.dataset.vc && !b.vcs.includes(i.dataset.vc)) b.vcs.push(i.dataset.vc);
  });
  document.querySelectorAll('.manual').forEach(inp => {
    const card = inp.closest('.grp'); const m = card ? card.id.match(/^boss-(\\d+)$/) : null;
    const b = m ? byIdx[+m[1]] : null;
    if (b && inp.value.trim()) {
      inp.value.split(/[,，\\s]+/).forEach(v => { v = v.trim(); if (v && !b.vcs.includes(v)) b.vcs.push(v); });
    }
  });
  const out = data.bosses.map(b => ({idx: b.idx, sku: b.sku, cn: b.cn, doublePrice: b.dp, vendorCodes: byIdx[b.idx].vcs}));
  const text = JSON.stringify(out, null, 2);
  document.getElementById('out').value = text;
  downloadJson(text, '核对结果.json');
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
function clearAll(){ document.querySelectorAll('.spick:checked,.gpick:checked').forEach(i=>i.checked=false); refresh(); }
function toggleMiss(){ document.querySelectorAll('.missrow').forEach(r=>r.style.display = r.style.display==='none'?'':'none'); }
refresh();
</script></body></html>'''

    data_json = {
        "bossCount": len([b for g in conflict_groups + single_groups for b in g["bosses"]]) + len(todo),
        "bosses": [{"idx": b["idx"], "sku": b["sku"], "cn": b["cn"], "dp": b["dp"]}
                   for g in conflict_groups + single_groups for b in g["bosses"]] +
                  [{"idx": b["idx"], "sku": b["sku"], "cn": b["cn"], "dp": b["dp"]} for b in todo],
    }
    html = html.replace("TODO_PLACEHOLDER", "\n".join(todo_html) if todo_html else '<div class="hint">无</div>')
    html = html.replace("CONFLICT_PLACEHOLDER", "\n".join(conflict_html) if conflict_html else '<div class="hint">无</div>')
    html = html.replace("SINGLE_PLACEHOLDER", "\n".join(single_html) if single_html else '<div class="hint">无</div>')
    html = html.replace("DATA_JSON", json.dumps(data_json, ensure_ascii=False))
    _write(config.OUT_MAPPING_HTML, html)
    return len(conflict_groups), len(single_groups), len(todo)


# ---------------- 统一核对工作台（5 店并集，一页两区） ----------------
def render_unified_html(conflict_groups, single_groups, todo, normal_items, shops_meta, stats=None):
    stats = stats or {}
    conflict_html = [_conflict_group_html(g) for g in conflict_groups]
    single_html = [_single_group_html(g) for g in single_groups]
    todo_html = [_todo_group_html(b) for b in todo]
    boss_opts = _boss_opts_html([b for g in conflict_groups + single_groups for b in g["bosses"]] + todo)
    cards_html = [_review_card_html(i, x, boss_opts) for i, x in enumerate(normal_items, 1)]

    shops_txt = ",".join(map(str, stats.get("shops") or [])) or "5 店"
    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>价格映射核对工作台（5 店统一）</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f0f2f5;color:#2c3e50}
.bar{position:sticky;top:0;background:#fff;padding:10px 16px;border-bottom:2px solid #3498db;z-index:9;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.bar b{color:#e74c3c}button{padding:6px 16px;cursor:pointer;border-radius:6px;border:1px solid #3498db;background:#3498db;color:#fff}
button.ghost{background:#fff;color:#3498db}
.wrap{padding:16px}
.grp{background:#fff;border:1px solid #ddd;border-radius:8px;margin:12px 0;padding:10px 14px}
.ghead{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.ghead b{font-size:15px}.idx{color:#7f8c8d;font-weight:bold}.sku{color:#2980b9}.dp{color:#e74c3c;font-weight:bold}
.kw{color:#95a5a6;font-size:12px}.cnt{color:#7f8c8d;font-size:12px}.hint{color:#8e44ad;font-size:12px;margin:4px 0}
.ok{color:#27ae60;font-size:12px}.picked{color:#27ae60;font-weight:bold}
.conflict .ghead{background:#fdf2e9;border-left:4px solid #e67e22;padding:4px 8px;border-radius:4px}
.todo .ghead{background:#fdedec;border-left:4px solid #c0392b;padding:4px 8px;border-radius:4px}
.gtable{border-collapse:collapse;width:100%}.gtable th{background:#f8f9fa;font-size:12px;padding:6px;border:1px solid #ddd}
.gtable td{border:1px solid #eee;vertical-align:top}.gtable .own{text-align:center;width:70px;font-size:12px}
.gtable .own label{display:block;padding:4px;cursor:pointer}
.gtable .own input:checked+span{color:#27ae60;font-weight:bold}
.missrow{opacity:.45}.missrow:hover{opacity:1}
.candtd{width:380px}.cand{display:flex;gap:8px;align-items:center;padding:4px}
.cand img{width:52px;height:52px;object-fit:contain;border-radius:4px;background:#fafafa;border:1px solid #eee}
.cand .ci{font-size:12px;line-height:1.4}.cand .vc{color:#7f8c8d}.cand .hit{color:#27ae60;font-size:11px}
.pickrow{display:flex;flex-wrap:wrap;gap:8px}
.pickw{display:flex;gap:6px;align-items:flex-start;border:1px solid #eee;border-radius:8px;padding:6px;cursor:pointer;width:300px}
.pickw:has(input:checked){outline:2px solid #27ae60;background:#f0fff4}
.pickw img{width:52px;height:52px;object-fit:contain;background:#fafafa;border-radius:4px}
.pickw .cand{flex:1}.misswrap{display:flex;flex-wrap:wrap;gap:8px}
.missbox summary{color:#95a5a6;cursor:pointer;font-size:12px;margin:6px 0}
.manual{width:60%;padding:6px;border:1px solid #ccc;border-radius:4px}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;margin:10px 0;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.card.excluded{opacity:.5;background:#f8f8f8}
.card .hd{display:flex;gap:10px;align-items:center;flex:1;min-width:0}
.card .hd .pimg{width:64px;height:64px;object-fit:contain;border-radius:6px;border:1px solid #eee;background:#fafafa}
.card .info{min-width:0}.card .tt{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:720px}
.card .vc{color:#7f8c8d;font-size:12px;margin-top:2px}
.card .shop{display:inline-block;background:#eee;border-radius:4px;padding:0 5px;margin-left:4px;font-size:11px}
.card .op select{min-width:300px;padding:6px;border:1px solid #ccc;border-radius:4px}
#out{width:100%;height:160px;font-family:Consolas,monospace;margin-top:8px}
.stats{color:#7f8c8d;font-size:13px}
</style></head>
<body>
<div class="bar">
  <b>价格映射核对工作台（5 店统一）</b>
  <span class="stats">店铺 [[SHOPS]] · 商品价格表商品 <b id="total"></b> · 候选池 <b>[[CAND]]</b> · 已归属 <b id="picked"></b>
    | 未归属新 vc <b>[[NORMAL]]</b> · 已映射 <b id="mapped"></b> · 已排除 <b id="excl"></b></span>
  <button onclick="exportJson()">导出核对结果 JSON</button>
  <button class="ghost" onclick="clearAll()">清空</button>
  <button class="ghost" onclick="toggleMiss()">显示/隐藏无关键词命中候选</button>
</div>
<div class="wrap">
<div class="hint" style="color:#2c3e50">
  <b>本工作台一次覆盖 5 店全部在架商品（按 vendorCode 去重），核对一次、合并一次即可。</b><br>
  上半区（一~三）：每个商品价格表商品勾选<b>实际对应的店铺候选</b>（看图片/俄文名/价格，可多选=同款多变体）；待核查可手工填 vendorCode。<br>
  下半区（四）：5 店在架但<b>价格未匹配到任何商品价格表商品</b>的 vendorCode——请为每个选择归属商品价格表商品（补录）或 🚫 非货盘（排除）。<br>
  核对完点「导出核对结果 JSON」→ 内容保存为 <b>统一审核.json</b> 发我 → <code>python wb.py merge 统一审核.json</code> 即完成。
</div>
<h3>一、待核查（价格带无候选，可手工填 vendorCode）</h3>
TODO_PLACEHOLDER
<h3>二、同价冲突组（候选池共享，人工图片区分归属）</h3>
CONFLICT_PLACEHOLDER
<h3>三、普通商品（候选通常唯一，勾选确认）</h3>
SINGLE_PLACEHOLDER
<h3>四、未归属新 vendorCode（[[NORMAL]] 个，价格未匹配任何商品价格表商品）</h3>
CARDS_PLACEHOLDER
<hr><h3>导出结果（vc 中心格式，merge 直接消费）：</h3><textarea id="out" placeholder="点上方按钮导出"></textarea>
</div>
<script>
const data = DATA_JSON;
document.getElementById('total').textContent = data.bossCount;
function refresh(){
  data.bosses.forEach(b => {
    let c = 0;
    document.querySelectorAll('.spick:checked').forEach(i => {
      const card = i.closest('.grp'); if (card && card.id === 'boss-' + b.idx) c++; });
    document.querySelectorAll('.gpick:checked').forEach(i => { if (+i.dataset.boss === b.idx) c++; });
    const el = document.querySelector('#boss-' + b.idx + ' .picked');
    if (el) el.textContent = c ? '已归属 ' + c + ' 个' : '';
  });
  let m = 0, e = 0;
  document.querySelectorAll('.card').forEach(cd => {
    const v = cd.querySelector('select').value;
    const isEx = v.startsWith('EXCLUDE');
    cd.classList.toggle('excluded', isEx);
    if (isEx) e++; else if (v) m++;
  });
  document.getElementById('picked').textContent =
    document.querySelectorAll('.spick:checked').length + document.querySelectorAll('.gpick:checked').length;
  document.getElementById('mapped').textContent = m;
  document.getElementById('excl').textContent = e;
}
document.querySelectorAll('.spick,.gpick,.assign').forEach(i => i.addEventListener('change', refresh));
function exportJson(){
  const out = [], seen = new Set(), byIdx = {};
  data.bosses.forEach(b => byIdx[b.idx] = b);
  const push = (vc, act, b) => {
    if (!vc || seen.has(vc)) return;
    seen.add(vc);
    if (act === 'mapped') out.push({vc, action: 'mapped', bossSku: b.sku, bossCn: b.cn, bossDp: b.dp});
    else out.push({vc, action: act});
  };
  document.querySelectorAll('.spick:checked').forEach(i => {
    const card = i.closest('.grp'); const m = card ? card.id.match(/^boss-(\\d+)$/) : null;
    if (m) push(i.dataset.vc, 'mapped', byIdx[+m[1]]);
  });
  document.querySelectorAll('.gpick:checked').forEach(i => push(i.dataset.vc, 'mapped', byIdx[+i.dataset.boss]));
  document.querySelectorAll('.manual').forEach(inp => {
    const card = inp.closest('.grp'); const m = card ? card.id.match(/^boss-(\\d+)$/) : null;
    if (!m) return;
    inp.value.split(/[,，\\s]+/).forEach(v => { v = v.trim(); if (v) push(v, 'mapped', byIdx[+m[1]]); });
  });
  document.querySelectorAll('.card').forEach(cd => {
    const v = cd.querySelector('select').value;
    if (!v) { push(cd.dataset.vc, 'pending'); return; }
    if (v.startsWith('EXCLUDE')) { push(cd.dataset.vc, 'exclude'); return; }
    const [sku, cn, dp] = v.split('|');
    push(cd.dataset.vc, 'mapped', {sku, cn, dp});
  });
  const text = JSON.stringify(out, null, 2);
  document.getElementById('out').value = text;
  downloadJson(text, '统一审核.json');
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
function clearAll(){ document.querySelectorAll('.spick:checked,.gpick:checked').forEach(i=>i.checked=false);
  document.querySelectorAll('.card select').forEach(s=>s.value=''); refresh(); }
function toggleMiss(){ document.querySelectorAll('.missrow').forEach(r=>r.style.display = r.style.display==='none'?'':'none'); }
refresh();
</script></body></html>'''
    html = html.replace("TODO_PLACEHOLDER", "\n".join(todo_html) if todo_html else '<div class="hint">无</div>')
    html = html.replace("CONFLICT_PLACEHOLDER", "\n".join(conflict_html) if conflict_html else '<div class="hint">无</div>')
    html = html.replace("SINGLE_PLACEHOLDER", "\n".join(single_html) if single_html else '<div class="hint">无</div>')
    html = html.replace("CARDS_PLACEHOLDER", "\n".join(cards_html) if cards_html else '<div class="hint">无</div>')
    html = html.replace("[[SHOPS]]", shops_txt).replace("[[CAND]]", str(stats.get("cand", 0)))\
               .replace("[[NORMAL]]", str(stats.get("normal", 0)))
    data_json = {
        "bossCount": len([b for g in conflict_groups + single_groups for b in g["bosses"]]) + len(todo),
        "bosses": [{"idx": b["idx"], "sku": b["sku"], "cn": b["cn"], "dp": b["dp"]}
                   for g in conflict_groups + single_groups for b in g["bosses"]] +
                  [{"idx": b["idx"], "sku": b["sku"], "cn": b["cn"], "dp": b["dp"]} for b in todo],
    }
    html = html.replace("DATA_JSON", json.dumps(data_json, ensure_ascii=False))
    _write(config.OUT_MAPPING_HTML, html)
    return len(conflict_groups), len(single_groups), len(todo), len(normal_items)


def _write(path, html):
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

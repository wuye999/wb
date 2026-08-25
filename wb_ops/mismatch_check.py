# -*- coding: utf-8 -*-
"""
wb_ops 货不对板筛查工作台（人工看图筛选「上架商品图片与中文名不符」的商品）

读映射表（已归属的中文名→vendorCode），按中文名分组带图展示：
- 同中文名商品放一起，一行一条，完整俄文标题 + 缩略图；
- 点击缩略图弹高清大图（/tm/ → /big/）；
- 点击标题即可勾选「货不对板」（勾选状态本地保存，刷新不丢）；
- 顶部可多选筛选中文名（单/多/全部）+ 搜索；
- 导出勾选的 vendorCode（JSON / 复制逗号分隔），用 wb.py trash --vc ... 下架。
"""
import datetime
import json
import os
import re
from collections import defaultdict
from html import escape

from . import config
from .mapping_check import load_mapping_rows


def render_html(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[r["cn"] or "（未命名）"].append(r)
    cn_list = sorted(groups.keys())

    group_html = []
    for cn in cn_list:
        items = groups[cn]
        row_html = []
        for r in items:
            img = escape(r["img"] or "")
            vc = escape(r["vc"])
            title = escape(r["title"] or "")
            price = r["price"]
            shops = escape(str(r["shops"] or ""))
            meta_bits = []
            if price not in (None, ""):
                meta_bits.append(f'主店价 <b>¥{price}</b>')
            if shops:
                meta_bits.append(f'店铺 {shops}')
            meta = " · ".join(meta_bits)
            row_html.append(f'''
<tr class="mrow" data-vc="{vc}">
  <td class="picktd"><input type="checkbox" class="badpick" data-vc="{vc}"
       title="勾选 = 货不对板"></td>
  <td class="imgtd"><img src="{img}" loading="lazy" class="thumb" onclick="showBig('{escape(img)}')"
       onerror="this.style.opacity=.12" title="点击查看大图"></td>
  <td class="titletd" onclick="toggleRow(this)">
    <div class="vc">{vc}</div>
    <div class="ru">{title}</div>
    <div class="meta">{meta}</div>
  </td>
</tr>''')
        group_html.append(f'''
<div class="grp" data-cn="{escape(cn)}">
  <div class="ghead"><b>{escape(cn)}</b> <span class="cnt">{len(items)} 个 vc</span></div>
  <table class="gtable">{"".join(row_html)}</table>
</div>''')

    cn_options = "\n".join(f'<option value="{escape(cn)}">{escape(cn)}</option>' for cn in cn_list)

    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>货不对板筛查工作台</title>
<style>
body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f0f2f5;color:#2c3e50}
.bar{position:sticky;top:0;background:#fff;padding:10px 16px;border-bottom:2px solid #c0392b;z-index:9;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.bar b{color:#c0392b}button{padding:6px 14px;cursor:pointer;border-radius:6px;border:1px solid #c0392b;background:#c0392b;color:#fff}
button.ghost{background:#fff;color:#c0392b}
input[type=search]{padding:6px 10px;border:1px solid #ccc;border-radius:6px;min-width:220px}
select[multiple]{padding:4px;border:1px solid #ccc;border-radius:6px;min-width:300px;min-height:180px;max-height:60vh}
.cnfilter{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cnfilter .cn-summary{color:#7f8c8d;font-size:12px}
.wrap{padding:16px}
.stats{color:#7f8c8d;font-size:13px}
.grp{background:#fff;border:1px solid #ddd;border-radius:8px;margin:12px 0;padding:10px 14px}
.ghead{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}
.ghead b{font-size:15px}.cnt{color:#7f8c8d;font-size:12px}
.gtable{border-collapse:collapse;width:100%}
.gtable td{border:1px solid #eee;vertical-align:top;padding:8px}
.gtable .picktd{width:40px;text-align:center;background:#fafafa}
.gtable .picktd input{width:18px;height:18px;cursor:pointer;accent-color:#c0392b}
.mrow.picked{background:#fdedec}
.mrow.picked .vc{color:#c0392b;font-weight:bold}
.mrow .titletd{cursor:pointer}
.mrow .titletd:hover{background:#fff5f3}
.gtable .imgtd{width:110px;text-align:center}
.gtable img{width:96px;height:96px;object-fit:contain;border-radius:6px;background:#fafafa;border:1px solid #eee}
.gtable .thumb{cursor:zoom-in;transition:transform .15s}
.gtable .thumb:hover{transform:scale(1.05);border-color:#c0392b}
#lightbox{position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;display:none;
  align-items:center;justify-content:center;flex-direction:column;cursor:zoom-out}
#lightbox.show{display:flex}
#lb-img{max-width:92vw;max-height:85vh;object-fit:contain;border-radius:8px;background:#fff;box-shadow:0 8px 40px rgba(0,0,0,.6)}
#lb-close{position:absolute;top:14px;right:20px;font-size:34px;color:#fff;cursor:pointer;line-height:1;opacity:.9}
#lb-close:hover{opacity:1;transform:scale(1.15)}
#lb-title{color:#fff;font-size:13px;margin-top:10px;max-width:90vw;text-align:center;word-break:break-all}
.vc{font-family:Consolas,monospace;font-size:12px;color:#7f8c8d}
.ru{font-size:13px;margin:2px 0;color:#2c3e50}
.meta{font-size:12px;color:#555;margin:2px 0}
.hint{color:#8e44ad;font-size:12px;margin:6px 0}
</style></head>
<body>
<div class="bar">
  <b>货不对板筛查工作台</b>
  <span class="stats">共 <b id="total"></b> 条 · <b id="groups"></b> 个中文名商品 · 已勾选 <b id="picked" style="color:#c0392b"></b> 条</span>
  <div class="cnfilter" id="cnWrap">
    <button id="cnToggle" class="ghost" onclick="toggleCn()" title="展开/收起中文名多选下拉">中文名筛选 ▸</button>
    <span id="cnSummary" class="cn-summary">全部</span>
    <div id="cnPanel" style="display:none">
      <select multiple size="12" id="cnSelect" onchange="applyFilter();syncCnSummary()">CN_OPTIONS</select>
    </div>
  </div>
  <input type="search" id="q" placeholder="搜索 vc / 中文名 / 俄文标题…" oninput="applyFilter()">
  <button id="btnPicked" class="ghost" onclick="togglePicked()">只看已勾选</button>
  <button class="ghost" onclick="exportPicked()">导出勾选 vc (JSON)</button>
  <button class="ghost" onclick="copyVcs()">复制 vc (逗号分隔)</button>
  <button class="ghost" onclick="clearPicked()">清空勾选</button>
</div>
<div class="wrap">
<div class="hint">
  <b>快速筛查货不对板</b>：逐个商品看图片与俄文标题是否与中文名一致；<b>点击标题（或左侧复选框）= 勾选货不对板</b>（勾选状态本地保存，刷新不丢）。
  顶部「中文名筛选」可多选只看某几个中文名（Ctrl/Cmd 点选，不选=全部）。核对完点「导出勾选 vc」或「复制 vc」，用 <code>wb.py trash --vc &lt;vc列表&gt; --apply --yes</code> 清空库存并移至回收站。
  （点击图片可看高清大图）
</div>
GROUPS_PLACEHOLDER
</div>
<div id="lightbox" onclick="closeBig(event)">
  <span id="lb-close" onclick="closeBig()">×</span>
  <img id="lb-img" src="" alt="">
  <div id="lb-title"></div>
</div>
<script>
const ROWS = ROWS_JSON;
document.getElementById('total').textContent = TOTAL;
document.getElementById('groups').textContent = GROUPS_N;
const LS_KEY = 'mismatch_check_picked_v2';
let onlyPicked = false;
function loadPicked(){
  try { return new Set(JSON.parse(localStorage.getItem(LS_KEY) || '[]')); } catch(e){ return new Set(); }
}
function savePicked(){
  document.querySelectorAll('.badpick').forEach(c => {
    c.closest('.mrow').classList.toggle('picked', c.checked);
  });
  const set = new Set();
  document.querySelectorAll('.badpick:checked').forEach(c => set.add(c.dataset.vc));
  localStorage.setItem(LS_KEY, JSON.stringify([...set]));
  refreshCount();
}
function refreshCount(){
  document.getElementById('picked').textContent = document.querySelectorAll('.badpick:checked').length;
}
document.querySelectorAll('.badpick').forEach(c => c.addEventListener('change', savePicked));
const prev = loadPicked();
if (prev.size){
  document.querySelectorAll('.badpick').forEach(c => {
    if (prev.has(c.dataset.vc)){ c.checked = true; c.closest('.mrow').classList.add('picked'); }
  });
  refreshCount();
}
function toggleRow(td){
  const cb = td.closest('.mrow').querySelector('.badpick');
  cb.checked = !cb.checked;
  savePicked();
}
function selectedCns(){
  const s = new Set();
  document.querySelectorAll('#cnSelect option:checked').forEach(o => s.add(o.value));
  return s;
}
function applyFilter(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const cns = selectedCns();
  document.querySelectorAll('.grp').forEach(g => {
    const cn = g.dataset.cn;
    const okCn = cns.size === 0 || cns.has(cn);
    let visible = false;
    g.querySelectorAll('.mrow').forEach(tr => {
      const txt = (tr.textContent || '').toLowerCase();
      const okQ = !q || txt.includes(q);
      const okP = !onlyPicked || tr.querySelector('.badpick').checked;
      tr.style.display = (okQ && okP) ? '' : 'none';
      if (okQ && okP) visible = true;
    });
    g.style.display = (visible && okCn) ? '' : 'none';
  });
}
function cnSummaryText(){
  const n = document.getElementById('cnSelect').selectedOptions.length;
  return n ? '已选 ' + n + ' 个中文名' : '全部';
}
function syncCnSummary(){ document.getElementById('cnSummary').textContent = cnSummaryText(); }
function toggleCn(){
  const p = document.getElementById('cnPanel');
  const t = document.getElementById('cnToggle');
  const showing = p.style.display !== 'none';
  p.style.display = showing ? 'none' : 'block';
  t.textContent = showing ? '中文名筛选 ▸' : '中文名筛选 ▾';
}
function togglePicked(){
  onlyPicked = !onlyPicked;
  document.getElementById('btnPicked').classList.toggle('ghost', onlyPicked);
  applyFilter();
}
function pickedRows(){
  const out = [];
  document.querySelectorAll('.badpick:checked').forEach(c => {
    const tr = c.closest('.mrow');
    const r = ROWS.find(x => x.vc === c.dataset.vc) || {};
    out.push({vc: c.dataset.vc, cn: r.cn || '', title: r.title || '', shops: r.shops || ''});
  });
  return out;
}
function exportPicked(){
  const rows = pickedRows();
  if (!rows.length){ alert('未勾选任何商品'); return; }
  const blob = new Blob([JSON.stringify(rows, null, 2)], {type:'application/json;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = '货不对板vc.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  alert('已下载 货不对板vc.json（' + rows.length + ' 条）');
}
function fallbackCopy(text, done, fail){
  // 兜底：临时可见 textarea（display:none 元素无法形成选区 → 复制为空）
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.top = '-9999px';
  ta.style.left = '0';
  document.body.appendChild(ta);
  ta.focus(); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch(e){ ok = false; }
  document.body.removeChild(ta);
  ok ? done() : fail();
}
function copyVcs(){
  const vcs = pickedRows().map(r => r.vc);
  if (!vcs.length){ alert('未勾选任何商品'); return; }
  const text = vcs.join(',');
  const done = () => alert('已复制 ' + vcs.length + ' 个 vc（逗号分隔，可直接用于 wb.py trash --vc）');
  const fail = () => alert('复制失败：请用「导出勾选 vc (JSON)」下载后再手动复制');
  if (navigator.clipboard && window.isSecureContext){
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done, fail));
    return;
  }
  fallbackCopy(text, done, fail);
}
function clearPicked(){
  document.querySelectorAll('.badpick:checked').forEach(c => c.checked = false);
  savePicked(); applyFilter();
}
function showBig(thumbUrl){
  const box = document.getElementById('lightbox');
  const img = document.getElementById('lb-img');
  const ttl = document.getElementById('lb-title');
  const big = thumbUrl.replace('/tm/', '/big/');
  img.onerror = function(){ if (this.src !== thumbUrl) this.src = thumbUrl; };
  img.src = big;
  ttl.textContent = big;
  box.classList.add('show');
}
function closeBig(ev){
  if (ev && ev.target.id !== 'lightbox' && ev.target.id !== 'lb-close') return;
  document.getElementById('lightbox').classList.remove('show');
  document.getElementById('lb-img').src = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeBig(); });
applyFilter();
</script></body></html>'''
    html = html.replace("CN_OPTIONS", cn_options)
    html = html.replace("GROUPS_PLACEHOLDER", "\n".join(group_html) if group_html else '<div class="hint">映射表为空</div>')
    html = html.replace("ROWS_JSON", json.dumps(
        [{"vc": r["vc"], "cn": r["cn"], "title": r["title"] or "", "shops": str(r.get("shops") or "")} for r in rows],
        ensure_ascii=False))
    html = html.replace("TOTAL", str(len(rows))).replace("GROUPS_N", str(len(groups)))
    os.makedirs(os.path.dirname(config.OUT_MISMATCH_HTML), exist_ok=True)
    with open(config.OUT_MISMATCH_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    return len(rows), len(groups)


def _to_date(v):
    """把映射表「创建时间」字段解析为 datetime.date；解析不出返回 None。
    兼容：datetime / date、字符串（YYYY-MM-DD 或带 HH:MM:SS / T 分隔 / 斜杠）、Excel 数字 serial、秒/毫秒时间戳。"""
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, (int, float)):
        try:
            if v > 1e10:  # 毫秒时间戳
                return datetime.datetime.fromtimestamp(v / 1000).date()
            if v > 1e8:   # 秒级时间戳
                return datetime.datetime.fromtimestamp(v).date()
        except (OverflowError, OSError, ValueError):
            pass
        try:  # Excel 串行日期（1899-12-30 起）
            return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(v))
        except (OverflowError, ValueError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.datetime.strptime(s[:19], fmt).date()
            except (ValueError, TypeError):
                continue
        m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except (ValueError, TypeError):
                return None
    return None


def _resolve_window(begin, end, days):
    """归一为 (start_date, end_date)，含边界。begin/end 为 'YYYY-MM-DD' 字符串。
    days N → end=今天, start=今天-(N-1)；给了 begin 优先 begin；end 缺省=今天；start 缺省=今天。"""
    today = datetime.date.today()
    start = None
    if begin:
        try:
            start = datetime.datetime.strptime(begin.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            start = None
    if start is None and days:
        start = today - datetime.timedelta(days=max(0, days - 1))
    if start is None:
        start = today
    end_date = today
    if end:
        try:
            end_date = datetime.datetime.strptime(end.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
    return start, end_date


def run(cn=None, begin="", end="", days=0):
    print(f"读取映射表 {config.MAPPING_XLSX} ...")
    rows = load_mapping_rows()
    if not rows:
        raise RuntimeError("映射表为空，请先 wb.py merge")
    if begin or end or days:
        win = _resolve_window(begin, end, days)
        kept, dropped = [], 0
        for r in rows:
            d = _to_date(r.get("create_at"))
            if d is None:
                dropped += 1  # 无创建时间 → 排除并提示
                continue
            if not (win[0] <= d <= win[1]):
                continue
            kept.append(r)
        if dropped:
            print(f"  [提示] 时间筛选生效：过滤了 {dropped} 行无创建时间")
        rows = kept
        print(f"  [时间段] 创建时间 {win[0]} ~ {win[1]} → {len(rows)} 条")
    if cn:
        hit = [r for r in rows if r["cn"] and str(r["cn"]) == cn]
        print(f"  按中文名过滤: 「{cn}」 → {len(hit)} 条")
        if not hit:
            print("  （无匹配，改用全量渲染）")
        else:
            rows = hit
    total, groups = render_html(rows)
    print(f"\n已生成: {config.OUT_MISMATCH_HTML}")
    print(f"  共 {total} 条 · {groups} 个中文名商品")
    print("  打开 HTML 逐个看图核对：点击标题/复选框勾选货不对板，顶部可多选筛选中文名。")
    print("  勾选后点「导出勾选 vc」或「复制 vc」，用 wb.py trash --vc <vc列表> --apply --yes 清空库存并移回收站。")

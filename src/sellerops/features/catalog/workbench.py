"""生成带图的本地待认领工作台。"""
import json
from html import escape
from pathlib import Path

from sellerops.features.catalog.mismatch_check import _image_cell
from sellerops.features.catalog.vc_mapping import pending_groups
from sellerops.persistence.mappings import MappingRepository
from sellerops.settings import paths


CATEGORIES = {'prefix': '前缀命中', 'multiplier': '倍数参考', 'unmatched': '待手动归属'}


def _money(price: int | float) -> str:
    return '¥' + str(price).removesuffix('.0')


def _card(group: dict, labels: dict[str, str]) -> str:
    items = group['items']
    photo = next((item for item in items if item['image_url']),
                 next((item for item in items if item['nm_id']), items[0]))
    image = _image_cell({'img': photo['image_url'], 'nm_id': photo['nm_id']})
    if photo['image_url'] or photo['nm_id']:
        image = ("<button class='picture' aria-label='查看商品大图' "
                 "onclick='showBig(this.querySelector(\"img\"))'>"
                 f"{image}</button>")
    title = escape(str(photo['title'] or items[0]['title']))
    prices = ' / '.join(_money(p) for p in sorted({i['price'] for i in items if i['price'] is not None}))
    shops = {s['shop_id']: s['shop_name'] or s['shop_id'] for item in items for s in item['shops']}
    shop_text = escape('、'.join(shops.values()))
    vcs = group['vcs']
    prefix = group['prefix_candidate']
    price_candidates = {}
    for item in items:
        for candidate in item['price_candidates']:
            price_candidates.setdefault(candidate['sku'], candidate)
    quick = []
    if prefix:
        quick.append(price_candidates.pop(prefix['sku'], prefix))
    quick.extend(list(price_candidates.values())[:5 - len(quick)])
    buttons = []
    for candidate in quick:
        label = labels[candidate['sku']]
        if candidate['minimum_price'] is not None:
            label += ' · 最低价' + _money(candidate['minimum_price'])
        if 'multiplier' in candidate:
            label += f" ×{candidate['multiplier']}"
        buttons.append(f'<button class="suggestion" data-sku="{escape(candidate["sku"])}">{escape(label)}</button>')
    search = ' '.join([*vcs, prices, *(p['cn'] for p in quick)])
    category = group['category']
    return f'''
<article class="card" data-vcs="{escape(json.dumps(vcs, ensure_ascii=False))}"
         data-category="{category}" data-prefix="{escape(prefix['sku'] if prefix else '')}"
         data-search="{escape(search)}">
  <div>{image}</div>
  <div class="details">
    <div><strong class="price">{prices or '现价未知'}</strong> <span class="category">{CATEGORIES[category]}</span></div>
    <div>店铺 {shop_text}</div>
    <details class="vcs"><summary>{len(vcs)} 个 VC · {escape(vcs[0])}</summary>
      <ul>{''.join(f'<li>{escape(vc)}</li>' for vc in vcs)}</ul>
    </details>
    <div class="actions">{''.join(buttons)}</div>
    <div class="actions">
      <input class="product" type="search" list="products" aria-label="按中文名选择归属" placeholder="搜索并选择中文名">
      <button class="exclude">非货盘（排除）</button>
      <button class="undo" hidden>撤销</button>
      <output class="choice" aria-live="polite">待处理</output>
    </div>
    <div class="ru" title="{title}">{title}</div>
  </div>
</article>'''


def render_html(groups: list[dict], products: list[dict]) -> str:
    options = []
    labels = {}
    for product in sorted(products, key=lambda item: (item['cn'], item['sku'])):
        cn, sku = product['cn'], product['sku']
        label = cn if product['name_unique'] else f'{cn}（{sku}）'
        labels[sku] = label
        options.append(f'<option data-sku="{escape(sku)}" data-cn="{escape(cn)}" value="{escape(label)}"></option>')
    return (_HEAD + '<datalist id="products">' + ''.join(options) + '</datalist>'
            + ''.join(_card(group, labels) for group in groups) + _END)


def run_workbench(args) -> int:
    products = MappingRepository.load_price_book(require_price=False)
    groups = pending_groups(products=products)
    output = Path(paths.current.out_mapping_html)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(groups, products), encoding='utf-8')
    counts = {key: sum(group['category'] == key for group in groups) for key in CATEGORIES}
    counts.update(groups=len(groups), vcs=sum(len(group['vcs']) for group in groups))
    args.output = {'artifacts': {'html': str(output)}, 'counts': counts}
    args.summary = f'已生成 {counts["groups"]} 组待认领商品工作台'
    args.next_action = '打开 HTML 看图归属，导出 JSON 后交回 WorkBuddy 或用 map batch-set --file 保存'
    print(f'{args.summary}：{output}')
    return 0


_HEAD = '''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>待认领商品工作台</title>
<style>
:root{color-scheme:light;--ink:#172736;--muted:#4b5e6e;--line:#ccd9e1;--accent:#145e68}
*{box-sizing:border-box}
body{margin:0;background:#f3f7f8;color:var(--ink);font:15px/1.55 system-ui,"Microsoft YaHei",sans-serif}
[hidden]{display:none!important}
.layout{max-width:1100px;margin:auto;padding:20px}
h1{font-size:clamp(24px,4vw,32px);margin:0 0 12px}
h2{font-size:16px;margin:0 0 4px}
p{margin:4px 0}
.overview,.guide,.toolbar,.card{background:#fff;border:1px solid var(--line);border-radius:12px}
.overview{padding:16px}
.overview-top,.bar,.actions,.tallies{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.overview-top{justify-content:space-between}
#stats{font-size:18px;font-weight:700}
progress{display:block;width:100%;height:12px;accent-color:var(--accent);margin:12px 0 8px}
.tallies{gap:8px 16px;color:var(--muted);font-variant-numeric:tabular-nums}
#storage-status,#export-status{display:block;color:var(--muted)}
#export-status:not(:empty){margin-top:12px}
#handoff{margin-top:12px;padding:12px;background:#eef5f6;border-radius:8px;overflow-wrap:anywhere}
#handoff textarea{display:block;width:100%;margin:6px 0;resize:vertical}
.steps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 24px;margin:14px 0 0;padding-left:22px}
.steps li::marker{color:var(--accent);font-weight:700}
.steps span,.hint{font-size:13px;color:var(--muted)}
.steps span{display:block}
.guide{margin:12px 0;padding:12px 16px}
.guide dl{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 18px;margin:8px 0}
.guide dt{font-weight:650}
.guide dd{margin:2px 0 0;color:var(--muted)}
.toolbar{position:sticky;top:0;z-index:1;margin-bottom:12px;padding:10px 12px}
.bar+.bar{margin-top:8px}
#search{flex:1 1 200px;min-width:0}
.actions{margin:8px 0}
button,input,select,textarea{font:inherit;border:1px solid #9cb1bf;border-radius:6px;padding:5px 9px;background:#fff;color:inherit}
button{cursor:pointer}
:focus-visible{outline:3px solid #196fa1;outline-offset:2px}
button[aria-pressed=true],#export{background:var(--accent);border-color:var(--accent);color:#fff}
.card{display:grid;grid-template-columns:150px minmax(0,1fr);gap:16px;
      border-inline-start:4px solid var(--line);margin:0 0 10px;padding:12px;overflow-wrap:anywhere}
.card[data-review="mapped"]{border-inline-start-color:#2d7850}
.card[data-review="excluded"]{border-inline-start-color:#b66d14}
.picture{padding:0;border:0;background:transparent;cursor:zoom-in}
.thumb,.image-unavailable{display:grid;place-items:center;width:150px;height:150px;object-fit:contain;
      border-radius:8px;background:#f0f3f8;color:#526078}
.category,.choice{font-size:13px;color:var(--muted)}
.choice{display:inline-block;border-radius:4px;background:#eff3f6;padding:2px 7px;margin-left:4px;font-weight:650}
.card[data-review="mapped"] .choice{background:#e6f4ea;color:#215c38}
.card[data-review="excluded"] .choice{background:#fff1da;color:#784708}
.price{font-size:22px;margin-right:8px}
.suggestion{background:#e8f1ff;border-color:#b3caf4;color:#154083;font-weight:600}
.exclude{color:#923529}.product{width:min(100%,235px)}
.vcs{font:13px/1.5 ui-monospace,monospace;color:#425370}.vcs summary{cursor:pointer}.vcs ul{margin:4px 0}
.ru{color:#536474;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#empty{padding:16px;text-align:center;color:var(--muted)}
dialog{border:0;border-radius:10px;padding:12px;text-align:right}dialog::backdrop{background:#000c}
dialog img{display:block;max-width:88vw;max-height:82vh;margin-top:8px;object-fit:contain}
@media(max-width:620px){.layout{padding:12px}.steps,.guide dl{grid-template-columns:1fr}.toolbar{position:static}#handoff textarea{min-height:8em}}
@media(max-width:460px){.card{grid-template-columns:1fr}}
</style>
<script>
function nextProductImage(img) {
  const remaining = JSON.parse(img.dataset.fallback);
  const next = remaining.shift();
  img.dataset.fallback = JSON.stringify(remaining);
  if (next) img.src = next;
  else {
    img.hidden = true;
    img.nextElementSibling.hidden = false;
  }
}
function showBig(thumb) {
  if (thumb.hidden) return;
  const box = document.getElementById('lightbox');
  const image = box.querySelector('img');
  const sources = [...new Set([thumb.dataset.big, ...JSON.parse(thumb.dataset.fallback), thumb.src])];
  image.hidden = false;
  image.nextElementSibling.hidden = true;
  image.dataset.fallback = JSON.stringify(sources.slice(1));
  image.src = sources[0];
  box.showModal();
}
</script>
</head>
<body>
<div class="layout">
<header>
  <h1>待认领商品工作台</h1>
  <div class="overview">
    <div class="overview-top">
      <span id="stats" role="status"></span>
      <button id="export">导出审核结果 JSON</button>
    </div>
    <progress id="progress" value="0" max="1" aria-label="商品组审核进度"></progress>
    <div class="tallies">
      <span>已归属 <strong id="mapped-count">0</strong></span>
      <span>已排除 <strong id="excluded-count">0</strong></span>
      <span>待处理 <strong id="pending-count">0</strong></span>
      <span id="shown-count"></span>
      <span id="storage-status" role="status"></span>
    </div>
    <p id="export-status" role="status"></p>
    <section id="handoff" aria-labelledby="handoff-title" hidden>
      <h2 id="handoff-title">把审核结果交回 WorkBuddy</h2>
      <p>把下载的 JSON 一起发送，并复制这段话：</p>
      <textarea readonly rows="2" aria-label="发给 WorkBuddy 的话术">这是我已确认的待认领审核结果。请按附件里的完整 VC 导入归属与排除记录，并告诉我导入了多少条。</textarea>
      <p>也可以在项目目录自行导入。请把示例路径换成文件的实际下载路径。</p>
      <code>python run.py map batch-set --file "实际下载路径/待认领审核.json" --json</code>
    </section>
    <ol class="steps">
      <li><strong>看图归属</strong><span>核对图片、店铺和完整 VC，确认对应商品。</span></li>
      <li><strong>导出审核结果 JSON</strong><span>可分批导出，每次包含所有已处理组，包括筛选隐藏的组。</span></li>
      <li><strong>交回 WorkBuddy</strong><span>发送下载的文件，再核对导入条数。</span></li>
    </ol>
  </div>
</header>
<main>
  <section class="guide" aria-labelledby="guide-title">
    <h2 id="guide-title">建议按以下顺序处理</h2>
    <dl>
      <div><dt>前缀命中</dt><dd>该组有明确的前缀建议。看图核对后采纳，也可另选商品。</dd></div>
      <div><dt>倍数参考</dt><dd>现价接近商品最低售价的正整数倍。结合图片核对，再决定归属。</dd></div>
      <div><dt>待手动归属</dt><dd>用商品中文名查找并选择，拿不准的留待复核。</dd></div>
    </dl>
    <p class="hint">确认商品不属于货盘时，点「非货盘（排除）」记录本地归属审核结果。</p>
  </section>
  <div class="toolbar">
    <div class="bar" id="filters" role="group" aria-label="筛选商品">
      <button data-filter="all">全部</button>
      <button data-filter="prefix">前缀命中</button>
      <button data-filter="multiplier">倍数参考</button>
      <button data-filter="unmatched">待手动归属</button>
      <select id="status" aria-label="处理状态">
        <option value="all">全部处理状态</option><option value="done">已处理</option><option value="todo">未处理</option>
      </select>
      <input type="search" id="search" aria-label="搜索 VC、中文名或价格" placeholder="搜索 VC、中文名或价格">
    </div>
    <div class="bar">
      <button id="accept-prefix" aria-describedby="prefix-scope">一键采纳全部前缀建议</button>
      <span class="hint" id="prefix-scope">仅处理尚未选择的前缀命中组，包括当前被筛选隐藏的组。</span>
    </div>
  </div>
'''


_END = '''<p id="empty" hidden></p>
</main>
</div>
<dialog id="lightbox" aria-label="商品大图">
  <form method="dialog"><button>关闭</button></form>
  <img alt="商品大图" onerror="nextProductImage(this)">
  <span class="image-unavailable" hidden>图片暂不可用</span>
</dialog>
<script>
const cards = [...document.querySelectorAll('.card')];
const filters = [...document.querySelectorAll('button[data-filter]')];
const options = [...document.querySelectorAll('#products option')];
const products = new Map(options.map(option => [option.dataset.sku, option]));
const byLabel = new Map(options.map(option => [option.value, option.dataset.sku]));
const status = document.getElementById('status');
const search = document.getElementById('search');
const progress = document.getElementById('progress');
const stats = document.getElementById('stats');
const empty = document.getElementById('empty');
const handoff = document.getElementById('handoff');
const exportStatus = document.getElementById('export-status');
const storageKey = 'sellerops-vc-workbench:' + location.pathname;
let choices = {};
let filter = 'all';
const storageWarning = () => document.getElementById('storage-status').textContent = '浏览器无法保存本次进度';
try {
  const saved = JSON.parse(localStorage.getItem(storageKey));
  if (saved && typeof saved === 'object' && !Array.isArray(saved)) choices = saved;
} catch { storageWarning(); }

function choiceFor(card) {
  const choice = choices[card.dataset.vcs];
  return choice?.action === 'exclude' || (choice?.action === 'map' && products.has(choice.sku)) ? choice : null;
}
function setChoice(card, choice) {
  if (choice) choices[card.dataset.vcs] = choice;
  else delete choices[card.dataset.vcs];
  const product = choice?.action === 'map' ? products.get(choice.sku) : null;
  card.dataset.review = product ? 'mapped' : choice ? 'excluded' : 'pending';
  card.querySelector('.product').value = product?.value || '';
  card.querySelector('.choice').textContent = product ? '已归属 · ' + product.value : choice ? '已排除 · 非货盘' : '待处理';
  card.querySelector('.undo').hidden = !choice;
}
function refresh() {
  let mapped = 0, excluded = 0, shown = 0;
  const counts = {all: cards.length, prefix: 0, multiplier: 0, unmatched: 0};
  const terms = search.value.trim().toLowerCase().split(/\\s+/);
  for (const card of cards) {
    const choice = choiceFor(card);
    const category = card.dataset.category;
    const label = products.get(choice?.sku)?.value || '';
    const haystack = (card.dataset.search + ' ' + label).toLowerCase();
    counts[category]++;
    if (choice?.action === 'map') mapped++;
    if (choice?.action === 'exclude') excluded++;
    card.hidden = (filter !== 'all' && filter !== category)
      || (status.value !== 'all' && (status.value === 'done') !== Boolean(choice))
      || !terms.every(term => haystack.includes(term));
    if (!card.hidden) shown++;
  }
  for (const button of filters) {
    button.textContent = button.dataset.label + ' ' + counts[button.dataset.filter];
    button.setAttribute('aria-pressed', button.dataset.filter === filter);
  }
  const done = mapped + excluded;
  progress.max = cards.length || 1;
  progress.value = done;
  stats.textContent = '已处理 ' + done + ' / ' + cards.length + ' 组';
  document.getElementById('mapped-count').textContent = mapped;
  document.getElementById('excluded-count').textContent = excluded;
  document.getElementById('pending-count').textContent = cards.length - done;
  document.getElementById('shown-count').textContent = '当前显示 ' + shown + ' 组';
  empty.textContent = cards.length ? '没有符合搜索或筛选条件的商品。' : '目前没有待认领商品。';
  empty.hidden = shown > 0;
}
function save() {
  handoff.hidden = true;
  exportStatus.textContent = '';
  try { localStorage.setItem(storageKey, JSON.stringify(choices)); }
  catch { storageWarning(); }
  refresh();
}
for (const button of filters) {
  button.dataset.label = button.textContent;
  button.onclick = () => { filter = button.dataset.filter; refresh(); };
}
for (const card of cards) {
  setChoice(card, choiceFor(card));
  const input = card.querySelector('.product');
  input.onchange = () => {
    const sku = byLabel.get(input.value.trim());
    if (sku) { setChoice(card, {action: 'map', sku}); save(); }
    else setChoice(card, choiceFor(card));
  };
  for (const button of card.querySelectorAll('.suggestion')) {
    button.onclick = () => { setChoice(card, {action: 'map', sku: button.dataset.sku}); save(); };
  }
  card.querySelector('.exclude').onclick = () => { setChoice(card, {action: 'exclude'}); save(); };
  card.querySelector('.undo').onclick = () => { setChoice(card, null); save(); };
}
status.onchange = refresh;
search.oninput = refresh;
document.getElementById('accept-prefix').onclick = () => {
  for (const card of cards) {
    if (card.dataset.prefix && !choiceFor(card)) setChoice(card, {action: 'map', sku: card.dataset.prefix});
  }
  save();
};
document.getElementById('export').onclick = () => {
  const processed = cards.filter(choiceFor);
  if (!processed.length) {
    handoff.hidden = true;
    exportStatus.textContent = cards.length ? '请先归属或排除至少一组商品，再导出审核结果。' : '目前没有待认领商品，无需导出。';
    return;
  }
  const results = processed.flatMap(card => {
    const choice = choiceFor(card);
    const product = choice.action === 'map' ? products.get(choice.sku) : null;
    return JSON.parse(card.dataset.vcs).map(vc => product
      ? {vc, action: 'map', sku: choice.sku, cn: product.dataset.cn}
      : {vc, action: 'exclude'});
  });
  const url = URL.createObjectURL(new Blob([JSON.stringify(results, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = '待认领审核.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  exportStatus.textContent = '已发起下载「' + link.download + '」，共 ' + processed.length
    + ' 组、' + results.length + ' 个完整 VC。请在下载目录找到文件。';
  handoff.hidden = false;
};
refresh();
</script>
</body>
</html>
'''

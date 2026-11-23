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
      <output class="choice" aria-live="polite"></output>
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
    args.next_action = '打开 HTML 看图归属，导出 JSON 后用 map batch-set --file 保存'
    print(f'{args.summary}：{output}')
    return 0


_HEAD = '''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>待认领商品工作台</title>
<style>
body{margin:0;background:#f4f6fa;color:#182336;font:15px/1.5 system-ui,"Microsoft YaHei",sans-serif}
[hidden]{display:none!important}
header{position:sticky;top:0;z-index:1;background:#fff;border-bottom:1px solid #d7deea}
header>div,main{max-width:1100px;margin:auto;padding:12px 20px}
h1{font-size:21px;margin:0}.bar,.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
button,input,select{font:inherit;border:1px solid #bbc8db;border-radius:6px;padding:5px 9px;background:#fff;color:inherit}
button{cursor:pointer}button[aria-pressed=true],#export{background:#23499e;border-color:#23499e;color:#fff}
.card{display:grid;grid-template-columns:150px minmax(0,1fr);gap:16px;background:#fff;border:1px solid #d7deea;
      border-radius:10px;margin:0 0 10px;padding:12px;overflow-wrap:anywhere}
.picture{padding:0;border:0;background:transparent;cursor:zoom-in}
.thumb,.image-unavailable{display:grid;place-items:center;width:150px;height:150px;object-fit:contain;
      border-radius:8px;background:#f0f3f8;color:#526078}
.category,.choice{font-size:13px;color:#425370}
.price{font-size:22px;margin-right:8px}
.suggestion{background:#e8f1ff;border-color:#b3caf4;color:#154083;font-weight:600}
.exclude{color:#923529}.product{width:min(100%,235px)}
.vcs{font:13px/1.5 ui-monospace,monospace;color:#425370}.vcs summary{cursor:pointer}.vcs ul{margin:4px 0}
.ru{color:#607089;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
dialog{border:0;border-radius:10px;padding:12px;text-align:right}dialog::backdrop{background:#000c}
dialog img{display:block;max-width:88vw;max-height:82vh;margin-top:8px;object-fit:contain}
@media(max-width:540px){.card{grid-template-columns:1fr}}
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
<header><div>
  <h1>待认领商品工作台</h1>
  <div><span id="stats" role="status"></span> <span id="storage-status" role="status"></span></div>
  <div class="bar" id="filters">
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
    <button id="accept-prefix">一键采纳全部前缀建议</button>
    <button id="export">导出审核结果 JSON</button>
  </div>
</div></header>
<main>
'''


_END = '''<p id="empty" hidden>没有符合条件的待认领商品。</p>
</main>
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
const storageKey = 'sellerops-vc-workbench:' + location.pathname;
let choices = {};
let filter = 'all';
const storageWarning = () => document.getElementById('storage-status').textContent = '浏览器无法保存本次进度';
try { choices = JSON.parse(localStorage.getItem(storageKey)) || {}; }
catch { storageWarning(); }

function choiceFor(card) {
  const choice = choices[card.dataset.vcs];
  return choice?.action === 'exclude' || products.has(choice?.sku) ? choice : null;
}
function setChoice(card, choice) {
  if (choice) choices[card.dataset.vcs] = choice;
  else delete choices[card.dataset.vcs];
  const product = products.get(choice?.sku);
  card.querySelector('.product').value = product?.value || '';
  card.querySelector('.choice').textContent = product ? '已归属：' + product.value : choice ? '已排除' : '';
  card.querySelector('.undo').hidden = !choice;
}
function refresh() {
  let done = 0, shown = 0;
  const counts = {all: cards.length, prefix: 0, multiplier: 0, unmatched: 0};
  const terms = search.value.trim().toLowerCase().split(/\\s+/);
  for (const card of cards) {
    const choice = choiceFor(card);
    const category = card.dataset.category;
    const label = products.get(choice?.sku)?.value || '';
    const haystack = (card.dataset.search + ' ' + label).toLowerCase();
    counts[category]++;
    if (choice) done++;
    card.hidden = (filter !== 'all' && filter !== category)
      || (status.value !== 'all' && (status.value === 'done') !== Boolean(choice))
      || !terms.every(term => haystack.includes(term));
    if (!card.hidden) shown++;
  }
  for (const button of filters) {
    button.textContent = button.dataset.label + ' ' + counts[button.dataset.filter];
    button.setAttribute('aria-pressed', button.dataset.filter === filter);
  }
  document.getElementById('stats').textContent = '共 ' + cards.length + ' 组 · 已处理 ' + done + ' · 未处理 ' + (cards.length - done) + ' · 当前显示 ' + shown;
  document.getElementById('empty').hidden = shown > 0;
}
function save() {
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
  const results = cards.flatMap(card => {
    const choice = choiceFor(card);
    if (!choice) return [];
    const product = products.get(choice.sku);
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
};
refresh();
</script>
</body>
</html>
'''

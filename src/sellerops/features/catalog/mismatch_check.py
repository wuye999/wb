"""按中文名生成已归属商品的大图筛查工作台。"""
import datetime
import json
import re
from collections import defaultdict
from html import escape
from pathlib import Path

from sellerops.domain.snapshot_values import first_price, positive_id
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.features.catalog import products
from sellerops.features.catalog.vc_mapping import multiplier_candidates
from sellerops.integrations.wildberries.client import basket_base
from sellerops.persistence.mappings import MappingRepository
from sellerops.settings import paths


_WB_IMAGE = re.compile(
    r'(https://basket-\d+\.wbbasket\.ru/vol\d+/part\d+/\d+/images/)'
    r'(?:tm|big|c\d+x\d+)(/\d+\.[^/]+)'
)


def load_mapping_rows() -> list[dict]:
    book = MappingRepository.current_products()
    state, _ = MappingRepository.load_mapping_state(products=book)
    shops_data, shops_meta = products.load_all_shops()
    shop_names = {shop['id']: shop['name'] or str(shop['id']) for shop in shops_meta}
    main_id = paths.MAIN_SHOP or shops_meta[0]['id']
    by_vc = defaultdict(dict)
    for shop_id, cards in shops_data.items():
        for card in cards:
            if (vc := card.get('vendorCode')) in state:
                by_vc[vc][shop_id] = card

    prefixes = defaultdict(list)
    for product in book:
        prefixes[product['prefix']].append(product)

    rows = []
    for vc, cards in by_vc.items():
        shop_ids = sorted(cards)
        card = cards.get(main_id, cards[shop_ids[0]])
        prices = {shop_id: price for shop_id in shop_ids if (price := first_price(cards[shop_id])) is not None}
        price = prices.get(main_id, next(iter(prices.values()), None))
        item = state[vc]
        shops = {shop_id: shop_names[shop_id] for shop_id in shop_ids}
        flags = []

        prefix = extract_vc_prefix(vc)
        owners = prefixes.get(prefix, [])
        if len(owners) == 1 and owners[0]['sku'] != item['sku']:
            owner = owners[0]
            flags.append(('prefix', '前缀冲突', f"{prefix} 登记为 {owner['cn']}（{owner['sku']}）"))

        minimum = item['minimum_price']
        matched = {}
        if minimum is not None:
            for current in set(prices.values()):
                if matches := multiplier_candidates(current, [item]):
                    matched[current] = matches[0]['multiplier']
            unmatched = [f'{shops[shop_id]}（{shop_id}）{_money(current)}'
                         for shop_id, current in prices.items() if current not in matched]
            if unmatched:
                detail = ' · '.join([f'最低售价 {_money(minimum)}', *unmatched])
                flags.append(('price', '非最低价倍数', detail))

        multipliers = sorted(set(matched.values())) if price in matched else []
        rows.append(dict(cn=item['cn'], vc=vc, price=price, minimum_price=minimum,
                         multipliers=multipliers, flags=flags, shops=shops, title=card.get('title') or '',
                         img=card.get('repImg'), nm_id=card.get('nmId') or card.get('nmID'),
                         create_at=card.get('createAt')))
    return rows


def _money(price: int | float) -> str:
    return '¥' + str(price).removesuffix('.0')


def _image_cell(row: dict) -> str:
    image = row.get('img')
    original = image.strip() if isinstance(image, str) else ''
    if not original:
        nm_id = positive_id(row.get('nm_id'))
        if nm_id is None:
            return '<span class="image-unavailable">暂无商品图</span>'
        original = f'{basket_base(nm_id)}/images/big/1.webp'

    preview = large = original
    if sizes := _WB_IMAGE.fullmatch(original):
        preview = sizes[1] + 'c246x328' + sizes[2]
        large = sizes[1] + 'big' + sizes[2]
    fallback = [url for url in dict.fromkeys((large, original)) if url != preview]
    return (
        f'<img src="{escape(preview)}" loading="lazy" decoding="async"'
        f' class="thumb" alt="商品主图" data-big="{escape(large)}"'
        f' data-fallback="{escape(json.dumps(fallback, ensure_ascii=False))}"'
        ' onerror="nextProductImage(this)">'
        '<span class="image-unavailable" hidden>图片暂不可用</span>'
    )


def _card(row: dict) -> str:
    vc = escape(row['vc'])
    title = escape(str(row['title']))
    shop_ids = ', '.join(map(str, row['shops']))
    shops = '、'.join(row['shops'].values())
    price = _money(row['price']) if row['price'] is not None else '现价未知'
    factors = '/'.join(f'×{factor}' for factor in row['multipliers'])
    multiple = f' · <span class="multiple">{factors}</span>' if factors else ''
    flags = ' '.join(f'<span class="badge {kind}" title="{escape(detail)}">{label}</span>'
                     for kind, label, detail in row['flags'])
    photo = _image_cell(row)
    zoom = '<button class="zoom" type="button" aria-label="查看商品大图" title="查看商品大图">🔍</button>'
    return f'''
<article class="product-card" data-vc="{vc}" data-shops="{shop_ids}" data-anomaly="{int(bool(flags))}">
  <label>
    <input class="pick" type="checkbox" aria-label="勾选货不对板：{vc}">
    <span class="check" aria-hidden="true">✓</span>
    <span class="photo">{photo}</span>
    <strong class="price">{price}{multiple}</strong>
    <span class="vc truncate" title="{vc}">{vc}</span>
    <span class="shop truncate" title="{escape(shops)}">{escape(shops)}</span>
    <span class="badges">{flags}</span>
    <span class="ru truncate" title="{title}">{title}</span>
  </label>
  {zoom if photo.startswith('<img') else ''}
</article>'''


def render_html(rows: list[dict]) -> tuple[str, dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[row['cn']].append(row)
    anomalies = {cn: sum(bool(row['flags']) for row in items) for cn, items in groups.items()}
    sections = []
    for cn in sorted(groups, key=lambda name: (not anomalies[name], name)):
        items = groups[cn]
        minimums = sorted({row['minimum_price'] for row in items if row['minimum_price'] is not None})
        minimum = ' / '.join(map(_money, minimums)) or '未知'
        warning = f' · <span class="warning">⚠ {anomalies[cn]} 个异常</span>' if anomalies[cn] else ''
        name = escape(cn)
        sections.append(f'''
<section class="group" data-cn="{name}">
  <h2>{name} <span>· 共 {len(items)} 个 · 最低售价 {minimum}{warning}</span></h2>
  <div class="gallery">{"".join(map(_card, items))}</div>
</section>''')

    counts = dict(total=len(rows), groups=len(groups), anomalies=sum(anomalies.values()))
    options = ''.join(f'<option value="{escape(cn)}">{escape(cn)}</option>' for cn in sorted(groups))
    stats = f'共 {counts["total"]} 条 · {counts["groups"]} 个中文名 · 异常 {counts["anomalies"]} 条'
    page = (_HEAD + f'''
<header>
  <h1>货不对板筛查工作台</h1>
  <p id="stats">{stats} · 已勾选 <strong id="picked-count">0</strong> 条</p>
  <div class="toolbar">
    <details class="names">
      <summary id="name-summary">中文名筛选</summary>
      <div><label for="cn-select">Ctrl / ⌘ 多选，不选为全部</label>
        <select id="cn-select" multiple size="8">{options}</select>
      </div>
    </details>
    <input type="search" id="search" aria-label="搜索商品" placeholder="搜索 VC / 中文名 / 俄文标题">
    <button id="only-anomalies" aria-pressed="false">只看异常</button>
    <button id="only-picked" aria-pressed="false">只看已勾选</button>
    <button id="export">导出勾选 vc (JSON)</button>
    <button id="copy">复制 vc (逗号分隔)</button>
    <button id="clear">清空勾选</button>
  </div>
  <output id="feedback" aria-live="polite" hidden></output>
</header>
<main>{"".join(sections)}<p id="empty" hidden>没有符合条件的商品</p></main>
''' + _END)
    return page, counts


def _to_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, (int, float)):
        try:
            if value > 1e8:
                timestamp = value / 1000 if value > 1e10 else value
                return datetime.datetime.fromtimestamp(timestamp).date()
            return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(value))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        match = re.match(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', value.strip())
        if match:
            try:
                return datetime.date(*map(int, match.groups()))
            except ValueError:
                return None
    return None


def _resolve_window(begin, end, days):
    today = datetime.date.today()

    def explicit_date(value, default):
        try:
            return datetime.datetime.strptime(value.strip(), '%Y-%m-%d').date()
        except (ValueError, AttributeError):
            return default

    start = explicit_date(begin, None)
    if start is None:
        start = today - datetime.timedelta(days=max(0, days - 1))
    return start, explicit_date(end, today)


def run(cn='', begin='', end='', days=0) -> dict:
    rows = load_mapping_rows()
    if begin or end or days:
        start, stop = _resolve_window(begin, end, days)
        rows = [row for row in rows if (created := _to_date(row['create_at'])) and start <= created <= stop]
    if cn:
        rows = [row for row in rows if row['cn'] == cn]

    page, counts = render_html(rows)
    output = Path(paths.current.out_mismatch_html)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding='utf-8')
    print(f'已生成 {output}\n共 {counts["total"]} 条 · {counts["groups"]} 个中文名 · 异常 {counts["anomalies"]} 条')
    return {'artifacts': {'html': str(output)}, 'counts': counts}


_HEAD = '''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>货不对板筛查工作台</title>
<style>
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:#f4f5f7;color:#242c35;font:14px/1.45 "Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:2;padding:12px 20px;background:#fff;border-bottom:1px solid #dce0e5}
h1{margin:0;font-size:21px}
#stats{margin:4px 0 10px;color:#525e6b}
#picked-count,.warning{color:#a92027}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
button,summary,input[type=search],select{border:1px solid #c4cbd4;border-radius:6px;background:#fff;color:inherit;font:inherit;padding:5px 9px}
button,summary{cursor:pointer}
button:hover,summary:hover,button[aria-pressed=true]{background:#edf0f4;border-color:#708093}
button[aria-pressed=true]{font-weight:bold}
.names{position:relative}
.names div{position:absolute;top:calc(100% + 4px);left:0;padding:8px;background:#fff;border:1px solid #c4cbd4;border-radius:6px}
.names label{display:block;font-size:12px;margin-bottom:4px;white-space:nowrap}
#cn-select{width:min(280px,75vw)}
#feedback{margin-top:6px;display:block;color:#525e6b}
main{padding:0 20px 18px}
.group{margin-top:16px;padding:12px;background:#fff;border:1px solid #dce0e5;border-radius:8px}
h2{margin:0 0 10px;font-size:17px}
h2 span{font-size:13px;font-weight:normal;color:#525e6b}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,190px),190px));gap:10px}
.product-card{position:relative;min-width:0;border:2px solid #e2e6eb;border-radius:8px;background:#fff}
.product-card:hover{border-color:#8e9aa8}
.product-card:has(.pick:checked){border-color:#bf272e;background:#fff0f0}
.product-card label{display:flex;flex-direction:column;gap:3px;height:100%;padding:7px;cursor:pointer}
.pick{position:absolute;width:1px;height:1px;opacity:0}
.product-card:has(.pick:focus-visible){outline:2px solid #2257a4;outline-offset:2px}
.check,.zoom{position:absolute;top:9px;border-radius:5px}
.check{visibility:hidden;left:9px;background:#bf272e;color:#fff;padding:1px 5px}
.pick:checked + .check{visibility:visible}
.zoom{right:9px;padding:3px 6px;background:#fff}
.photo{display:grid;place-items:center;width:min(100%,170px);aspect-ratio:1;margin:0 auto 3px;border-radius:5px;background:#f5f6f8}
.thumb{display:block;width:100%;height:100%;object-fit:contain}
.image-unavailable{color:#626d7a;font-size:12px}
.price{font-size:16px}
.multiple{font-size:13px;color:#525e6b}
.truncate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vc{font:12px/1.4 Consolas,monospace}
.shop,.badge{padding:1px 5px;border-radius:4px;font-size:11px}
.shop{align-self:flex-start;max-width:100%;background:#edf0f4}
.badges:empty{display:none}
.prefix{background:#fde5e5;color:#a92027}
.price.badge{background:#fff0bf;color:#674700;font-size:11px}
.ru{margin-top:auto;color:#606b78;font-size:11px}
dialog{position:relative;max-width:96vw;padding:12px;border:0;border-radius:8px;text-align:center}
dialog::backdrop{background:#000c}
dialog img{display:block;max-width:90vw;max-height:82vh;object-fit:contain}
#close{position:absolute;top:8px;right:8px;font-size:20px}
@media(max-width:450px){
  header,main{padding-left:8px;padding-right:8px}
  .group{padding:8px}
  .gallery{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
}
</style>
<script>
function nextProductImage(img){
  const fallback = JSON.parse(img.dataset.fallback);
  const next = fallback[Number(img.dataset.attempt || 0)];
  if (next){
    img.dataset.attempt = Number(img.dataset.attempt || 0) + 1;
    img.src = next;
  } else {
    img.hidden = true;
    img.nextElementSibling.hidden = false;
  }
}
</script>
</head>
<body>'''


_END = '''
<dialog id="lightbox" aria-label="商品大图">
  <button id="close" aria-label="关闭大图">×</button>
  <img alt="商品大图" onerror="nextProductImage(this)">
  <span class="image-unavailable" hidden>图片暂不可用</span>
</dialog>
<script>
const groups = [...document.querySelectorAll('.group')];
const cards = [...document.querySelectorAll('.product-card')];
const names = document.getElementById('cn-select');
const search = document.getElementById('search');
const anomalyFilter = document.getElementById('only-anomalies');
const pickedFilter = document.getElementById('only-picked');
const feedback = document.getElementById('feedback');
const lightbox = document.getElementById('lightbox');
const storageKey = 'mismatch_check_picked_v2';
let picked = new Set();
try { picked = new Set(JSON.parse(localStorage.getItem(storageKey) || '[]')); } catch {}

function announce(message){
  feedback.textContent = message;
  feedback.hidden = !message;
}
function save(){
  try { localStorage.setItem(storageKey, JSON.stringify([...picked])); }
  catch { announce('浏览器无法保存勾选状态'); }
  refresh();
}
function refresh(){
  const selectedNames = new Set([...names.selectedOptions].map(option => option.value));
  const query = search.value.trim().toLowerCase();
  const onlyAnomalies = anomalyFilter.getAttribute('aria-pressed') === 'true';
  const onlyPicked = pickedFilter.getAttribute('aria-pressed') === 'true';
  let count = 0;
  let anyVisible = false;
  for (const group of groups){
    const cn = group.dataset.cn;
    let visible = false;
    for (const card of group.querySelectorAll('.product-card')){
      const checked = picked.has(card.dataset.vc);
      card.querySelector('.pick').checked = checked;
      count += Number(checked);
      card.hidden = (selectedNames.size > 0 && !selectedNames.has(cn))
        || (onlyAnomalies && card.dataset.anomaly !== '1')
        || (onlyPicked && !checked)
        || ![card.dataset.vc, cn, card.querySelector('.ru').textContent]
          .some(text => text.toLowerCase().includes(query));
      visible ||= !card.hidden;
    }
    group.hidden = !visible;
    anyVisible ||= visible;
  }
  document.getElementById('picked-count').textContent = count;
  document.getElementById('empty').hidden = anyVisible;
  document.getElementById('name-summary').textContent = selectedNames.size
    ? '中文名筛选 · 已选 ' + selectedNames.size + ' 个' : '中文名筛选';
}
function pickedRows(){
  return cards.filter(card => picked.has(card.dataset.vc)).map(card => ({
    vc: card.dataset.vc, cn: card.closest('.group').dataset.cn,
    title: card.querySelector('.ru').textContent, shops: card.dataset.shops
  }));
}
function showBig(thumb){
  const image = lightbox.querySelector('img');
  const sources = [...new Set([thumb.dataset.big, ...JSON.parse(thumb.dataset.fallback), thumb.src])];
  image.hidden = false;
  image.nextElementSibling.hidden = true;
  image.dataset.attempt = 0;
  image.dataset.fallback = JSON.stringify(sources.slice(1));
  image.src = sources[0];
  lightbox.showModal();
}
for (const card of cards){
  const checkbox = card.querySelector('.pick');
  card.onclick = event => { if (event.target === card) checkbox.click(); };
  checkbox.onchange = () => {
    if (checkbox.checked) picked.add(card.dataset.vc);
    else picked.delete(card.dataset.vc);
    save();
  };
  const zoom = card.querySelector('.zoom');
  if (zoom) zoom.onclick = event => {
    event.stopPropagation();
    showBig(card.querySelector('.thumb'));
  };
}
document.getElementById('close').onclick = () => lightbox.close();
lightbox.onclick = event => { if (event.target === lightbox) lightbox.close(); };
names.onchange = refresh;
search.oninput = refresh;
for (const filter of [anomalyFilter, pickedFilter]){
  filter.onclick = () => {
    filter.setAttribute('aria-pressed', filter.getAttribute('aria-pressed') !== 'true');
    refresh();
  };
}
document.getElementById('export').onclick = () => {
  const rows = pickedRows();
  if (!rows.length) return announce('未勾选任何商品');
  const url = URL.createObjectURL(new Blob([JSON.stringify(rows, null, 2)], {type:'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = '货不对板vc.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
document.getElementById('copy').onclick = async () => {
  const vcs = pickedRows().map(row => row.vc);
  if (!vcs.length) return announce('未勾选任何商品');
  const text = vcs.join(',');
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch {
    const input = document.createElement('textarea');
    input.value = text;
    input.style.position = 'fixed';
    input.style.left = '-9999px';
    document.body.append(input);
    input.select();
    try { copied = document.execCommand('copy'); } catch {}
    input.remove();
  }
  announce(copied ? '已复制 ' + vcs.length + ' 个完整 VC' : '复制失败，请导出勾选 VC');
};
document.getElementById('clear').onclick = () => {
  picked.clear();
  announce('');
  save();
};
refresh();
</script>
</body>
</html>
'''

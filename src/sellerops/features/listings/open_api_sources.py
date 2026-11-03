"""任意 WB 商品码的公开 CDN 详情 → 官方建卡模板。

官方 Content 只能读取本店商品，其他卖家商品从公开 CDN 读取；不走 BCS 采集代理。
"""
import copy
import math
import re
from typing import Any

from sellerops.core.files import safe_load_json
from sellerops.features.listings.open_api_common import split_values
from sellerops.features.listings.open_api_payloads import positive_id
from sellerops.features.listings.product_details import fetch_card_json, parse_package_info
from sellerops.features.listings.publish_details import generate_image_urls
from sellerops.features.listings.publish_inputs import parse_dims_str


def public_template(nm: int, vc: str, *, dims: str = '', overrides: dict[str, Any] | None = None
                    ) -> tuple[dict[str, Any], dict[str, str]]:
    raw = fetch_card_json(nm)
    if not isinstance(raw, dict) or not raw:
        raise ValueError('公开 card.json 读取失败，不回退 BCS')
    extra = copy.deepcopy(overrides or {})
    if not isinstance(extra, dict) or {'nmID', 'nmId', 'vendorCode'} & extra.keys():
        raise ValueError('card 覆盖须为对象，商品码和 VC 在外层指定')
    data = raw.get('data') or {}
    subject = positive_id(extra.pop('subjectID', None) or raw.get('subject_id') or data.get('subject_id'))
    pkg = parse_package_info(raw)
    if dims:
        parsed = parse_dims_str(dims)
        if not parsed:
            raise ValueError('--dims 格式应为 长*宽*高/毛重')
        pkg = dict(zip(('length', 'width', 'height', 'weight'), parsed))
    count = (raw.get('media') or {}).get('photo_count')
    if type(count) is not int or not 1 <= count <= 30:
        raise ValueError('公开商品缺少有效图片数量，不猜测图片范围')
    template = {
        'nmID': nm, 'subjectID': subject, 'vendorCode': vc,
        'title': raw.get('imt_name'), 'description': raw.get('description', ''),
        'brand': (raw.get('selling') or {}).get('brand_name') or '',
        'dimensions': {'length': pkg['length'], 'width': pkg['width'], 'height': pkg['height'],
                       'weightBrutto': pkg['weight']},
        'photos': [{'big': url} for url in generate_image_urls(nm, count)], 'video': '',
        'sizes': [{'techSize': '0'}],
    }
    template.update(extra)
    options: dict[str, str] = {}
    rows = list(raw.get('options') or [])
    for group in raw.get('grouped_options') or []:
        rows.extend(group.get('options') or [])
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get('name'), str) and row.get('value') is not None:
            options[row['name'].strip().casefold()] = str(row['value']).strip()
    return template, options


def fill_public_characteristics(card: dict[str, Any], options: dict[str, str],
                                schema: list[dict[str, Any]]) -> None:
    if 'characteristics' in card:
        return
    values = []
    for spec in schema:
        value = options.get(str(spec.get('name') or '').strip().casefold())
        if not value or spec.get('existNamedField'):
            continue
        if spec.get('charcType') == 4:
            unit = str(spec.get('unitName') or '').strip()
            number = value[:-len(unit)].strip() if unit and value.endswith(unit) else value
            if not re.fullmatch(r'\d+(?:[.,]\d+)?', number):
                continue
            parsed = float(number.replace(',', '.'))
            if not math.isfinite(parsed):
                continue
            result: Any = parsed
        else:
            result = [value]
        values.append({'id': positive_id(spec.get('charcID')), 'value': result})
    card['characteristics'] = values


def public_inputs(args: Any) -> list[dict[str, Any]]:
    rows = []
    if args.file:
        loaded = safe_load_json(args.file)
        if not isinstance(loaded, list) or not loaded or any(not isinstance(r, dict) for r in loaded):
            raise ValueError('--file 须为非空 JSON 对象数组')
        rows.extend(copy.deepcopy(loaded))
    if args.nm:
        nms = [positive_id(v) for v in split_values(args.nm)]
        vcs = split_values(args.vc) if args.vc and '{nm}' not in args.vc else []
        if vcs and len(vcs) != len(nms):
            raise ValueError('公开商品的 --vc 数量须与 --nm 数量一致，或使用含 {nm} 的模板')
        if not args.vc:
            raise ValueError('任意商品码上架须显式提供完整 --vc 或含 {nm} 的模板')
        for i, nm in enumerate(nms):
            vc = args.vc.replace('{nm}', str(nm)) if '{nm}' in args.vc else vcs[i]
            rows.append({'nmId': nm, 'vendorCode': vc})
    if not rows:
        raise ValueError('任意商品码来源需要 --nm 或 --file')
    if args.limit < 0:
        raise ValueError('--limit 不能为负')
    return rows[:args.limit] if args.limit else rows

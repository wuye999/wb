"""publish details。"""
import copy
from collections import defaultdict


import math
import re


from typing import Dict, Any, List, Optional, Tuple
from sellerops.domain.models import PublishItem


from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository


from sellerops.features.publishing.product_details import fetch_card_json
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.domain.snapshot_values import first_price
from sellerops.features.publishing.product_details import random_prefix
from sellerops.features.publishing.product_details import resolve_replicate_package
from sellerops.integrations.wildberries.client import basket_base
from sellerops.features.publishing.publish_inputs import DEFAULT_STOCK

def generate_image_urls(nm_id: int, photo_count: Optional[int] = None) -> List[str]:
    """生成 WB 商品 CDN 高清大图链接列表"""
    count = photo_count or 1
    urls = [f"{basket_base(nm_id)}/images/big/{i}.webp" for i in range(1, count + 1)]
    if not urls:
        urls = [f"{basket_base(nm_id)}/images/big/1.webp"]
    return urls

def build_synthetic_detail(
    card_info: Optional[Dict[str, Any]],
    nm_id: int,
    title: str = "",
    weight: float = 0.0,
    vendor_code: str = "",
    price_str: str = "0.00",
    subject_id: Optional[int] = None,
    parent_id: Optional[int] = None,
) -> Dict[str, Any]:
    """根据 card.json 组装 BCS 完整 VC 建卡所需的 wbDetail。"""
    ci = card_info or {}
    data = ci.get("data") or {}
    subj_id = (
        subject_id
        or ci.get("subjectId")
        or ci.get("subject_id")
        or data.get("subject_id")
        or 0
    )
    p_id = (
        parent_id
        or ci.get("subjectParentId")
        or ci.get("subject_root_id")
        or data.get("subject_root_id")
        or 0
    )
    sizes = [{"origName": 0, "name": "", "price": price_str, "vendorCode": vendor_code}]
    return {
        "id": int(nm_id),
        "root": ci.get("imt_id") or 0,
        "brand": (ci.get("selling") or {}).get("brand_name") or "",
        "brandId": 0,
        "colors": ci.get("colors") or [],
        "subjectId": subj_id,
        "subjectParentId": p_id,
        "name": title or ci.get("imt_name") or "",
        "pics": (ci.get("media") or {}).get("photo_count") or 0,
        "weight": weight or 0,
        "sizes": sizes,
    }

def resolve_publish_details(
    item: PublishItem,
    mapping_state: Optional[Dict[str, Any]] = None,
    price_book: Optional[List[dict]] = None,
    card_cache: Optional[Dict[int, Any]] = None,
    *, generated: dict | None = None, entries: dict | None = None,
) -> Tuple[Optional[PublishItem], Optional[str]]:
    """
    智能补全上架商品缺失字段：
    1. 校验 nm_id
    2. 中文名自动补全（若空则查本地映射表）
    3. 价格自动补全（指定价格 > 本地快照价）
    4. 使用显式完整 VC、指定前缀、卖家 SKU 的唯一登记前缀或明确允许的随机前缀
    5. 尺寸重量自动补全（显式传入 > 价格映射表 > 价格表 > card.json 包装组）
    6. 标题与图片提取（从 card.json 获取并组装）
    返回: (resolved_item, None) 或 (None, err_msg)
    """
    nm_id = item.nm_id
    if not nm_id or nm_id <= 0:
        return None, f"无效的 WB商品码: {nm_id}"

    card_cache = card_cache if card_cache is not None else {}
    if price_book is None:
        price_book = MappingRepository.load_price_book(require_price=False, fresh=True)
    if mapping_state is None:
        mapping_state, _ = MappingRepository.load_mapping_state(products=price_book)

    def confirmed_sku(code):
        if not code:
            return None
        row = db.resolve_vc(code, entries)
        return row['sku'] or (mapping_state.get(code) or {}).get('sku')

    # 优先使用完整 VC 和源卡的已确认归属。
    vc = (item.vendor_code or '').strip()
    source_card = ProductSnapshotRepository.find_source(nm_id, item.extra.get('src_sid'))
    source_card = source_card or {}
    source_vc = str(source_card.get('vendorCode') or source_card.get('goodsCode') or '').strip()
    matched_map_row = mapping_state.get(vc) or mapping_state.get(source_vc) or {}
    confirmed = {value for value in (item.extra.get('seller_sku'), confirmed_sku(vc),
                                     confirmed_sku(source_vc)) if value}
    if len(confirmed) > 1:
        return None, '指定的卖家 SKU 与已有归属冲突，请先核对源商品与目标 VC'
    sku = next(iter(confirmed), None)
    if sku and not any(row['sku'] == sku for row in price_book):
        return None, f'卖家 SKU {sku} 已无现行价格表资料，请核对商品归属'
    matches = [row for row in price_book if (row['sku'] == sku if sku else
               row['cn'] == item.cn.strip() and row.get('name_unique', True))]
    if len(matches) != 1:
        return None, '无法确认唯一商品，请提供 --sku 或使用唯一中文名 --cn'
    product = matches[0]
    sku, cn = product['sku'], product['cn']

    prefix = (item.prefix or '').strip().upper().removeprefix('BCS-')
    if vc:
        prefix = extract_vc_prefix(vc) or 'CUSTOM'
        vc_source = 'explicit_vc'
    elif prefix:
        if not re.fullmatch(r'[A-Z]{4}', prefix):
            return None, '请通过 --prefix 指定四个英文字母'
        vc_source = 'explicit_prefix'
    else:
        registered = product['prefix']
        if re.fullmatch(r'[A-Z]{4}', registered or '') and sum(
                row['prefix'] == registered for row in price_book) == 1:
            prefix, vc_source = registered, 'registered_prefix'
        elif item.extra.get('random_prefix'):
            generated = {} if generated is None else generated
            key = (nm_id, sku)
            if key not in generated:
                generated[key] = random_prefix()
            prefix, vc_source = generated[key], 'random'
        else:
            return None, f'商品 {cn} 没有唯一登记前缀，请指定 --prefix 或明确选择 --random-prefix'
    prefix = f'BCS-{prefix}'
    vc = vc or f'{prefix}-{item.extra.get("source_tail") or nm_id}'
    if confirmed_sku(vc) not in (None, sku):
        return None, f'完整 VC {vc} 已有其他卖家 SKU 归属，请先核对'

    # 3. 价格决策
    price = item.price
    if price is None or price <= 0:
        price = first_price(source_card)
    if price is None or not math.isfinite(price) or price <= 0:
        return None, f"商品 {nm_id} 价格缺失且无法从映射表自动匹配，请通过 --price 指定"

    # 4. 包装尺寸与重量决策
    dimensions = {
        **matched_map_row, 'sku': sku, 'dimensions': product.get('dimensions'),
        'L': item.length or matched_map_row.get('L'), 'W': item.width or matched_map_row.get('W'),
        'H': item.height or matched_map_row.get('H'), 'weight': item.weight or matched_map_row.get('weight'),
    }
    card_info = item.extra.get('card_info')
    if card_info:
        card_cache[nm_id] = card_info
    dims, error = resolve_replicate_package(vc, cn, nm_id, {vc: dimensions}, card_cache)
    if error or not dims:
        return None, f'商品 {nm_id} 包装尺寸或毛重缺失，请通过 --dims 指定'
    l, w, h, wt = dims

    if nm_id not in card_cache:
        card_cache[nm_id] = fetch_card_json(nm_id)
    card_info = card_cache[nm_id]

    title = item.title or (card_info.get("imt_name") if card_info else "") or ""
    photo_count = ((card_info.get("media") or {}).get("photo_count")) if card_info else 1
    gen_imgs = ";".join(generate_image_urls(nm_id, photo_count))
    images = item.images or gen_imgs
    main_image = item.main_image or images.split(";")[0]

    resolved = PublishItem(
        nm_id=nm_id,
        price=round(float(price), 2),
        vendor_code=vc,
        prefix=prefix,
        length=l,
        width=w,
        height=h,
        weight=wt,
        stock=item.stock,
        target_shops=copy.deepcopy(item.target_shops),
        cn=cn,
        title=title,
        main_image=main_image,
        images=images,
        extra={
            **item.extra,
            "card_info": card_info,
            'seller_sku': sku,
            'vc_source': vc_source,
            'source_vc': source_vc,
        }
    )
    return resolved, None


def resolve_publish_items(args, items, *, bulk=False):
    """准备两个普通上架入口共用的商品信息和受阻明细。"""
    book = MappingRepository.load_price_book(require_price=False, fresh=True)
    mapping_state, _ = MappingRepository.load_mapping_state(products=book)
    entries = db.vc_entries()
    card_cache, generated = {}, {}
    owners = defaultdict(set)
    resolved_items, blocked = [], []
    for item in items:
        resolved, error = resolve_publish_details(item, mapping_state, book, card_cache, generated=generated,
                                                  entries=entries)
        if resolved and bulk and resolved.vendor_code and resolved.vendor_code != f'{resolved.prefix}-{resolved.nm_id}':
            error = f'批量接口无法保留完整 VC {resolved.vendor_code}，请用 ops create-cards 预览'
        if error or not resolved:
            blocked.append({'nm_id': item.nm_id, 'reason': error or '商品信息不完整'})
            print(f'[跳过] 商品 {item.nm_id}: {blocked[-1]["reason"]}')
        else:
            resolved_items.append(resolved)
            owners[resolved.vendor_code].add(resolved.extra.get('seller_sku'))
    if conflicts := {vc for vc, skus in owners.items() if len(skus) > 1}:
        blocked.extend({'nm_id': item.nm_id, 'reason': f'完整 VC {item.vendor_code} 同时指定了多个卖家 SKU'}
                       for item in resolved_items if item.vendor_code in conflicts)
        resolved_items = [item for item in resolved_items if item.vendor_code not in conflicts]
    if blocked:
        args.outcome = {'blocked': blocked}
        args.next_action = blocked[0]['reason']
        if not resolved_items:
            args.summary = f'商品 {blocked[0]["nm_id"]} 未通过上架检查'
    return resolved_items, len(blocked)

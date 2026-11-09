"""publish details。"""
import copy


import math


from typing import Dict, Any, List, Optional, Tuple
from sellerops.domain.models import PublishItem


from sellerops.persistence.mappings import MappingRepository


from sellerops.features.listings.product_details import fetch_card_json
from sellerops.features.listings.product_details import parse_package_info
from sellerops.domain.vendor_codes import extract_vc_prefix
from sellerops.features.listings.product_details import resolve_vendor_prefix
from sellerops.features.listings.product_details import resolve_replicate_package
from sellerops.integrations.wildberries.client import basket_base
from sellerops.features.listings.publish_inputs import DEFAULT_STOCK

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
    pmap: Optional[Dict[str, Any]] = None,
    boss_pkg: Optional[Dict[str, Any]] = None,
    card_cache: Optional[Dict[int, Any]] = None,
) -> Tuple[Optional[PublishItem], Optional[str]]:
    """
    智能补全上架商品缺失字段：
    1. 校验 nm_id
    2. 中文名自动补全（若空则查本地映射表）
    3. 价格自动补全（指定价格 > 映射表在架价/快照价 > 价格表实际售价 floor）
    4. 前缀码与完整 VC 自动推导（指定完整 VC 优先；或指定前缀码；或中文名价格表前缀；或随机 4 位）
    5. 尺寸重量自动补全（显式传入 > 价格映射表 > 价格表 > card.json 包装组）
    6. 标题与图片提取（从 card.json 获取并组装）
    返回: (resolved_item, None) 或 (None, err_msg)
    """
    nm_id = item.nm_id
    if not nm_id or nm_id <= 0:
        return None, f"无效的 WB商品码: {nm_id}"

    card_cache = card_cache if card_cache is not None else {}
    mapping_state = mapping_state if mapping_state is not None else {}
    boss_pkg = boss_pkg if boss_pkg is not None else {}
    pmap = pmap if pmap is not None else {}

    # 构建快速 nmId -> 映射表行索引
    nm_str = str(nm_id)
    matched_map_row = None
    for vc_k, row in mapping_state.items():
        row_nm = str(row.get("nmId") or "").strip()
        if row_nm == nm_str or vc_k == nm_str or vc_k.endswith(f"-{nm_str}"):
            matched_map_row = row
            break

    # 1. 中文名补全
    cn = item.cn.strip()
    if not cn and matched_map_row:
        cn = matched_map_row.get("cn") or ""

    # 2. 完整 VC 与前缀码决策
    vc = (item.vendor_code or "").strip()
    prefix = (item.prefix or "").strip()

    if vc:
        # 指定了完整 VC
        if not prefix:
            extracted = extract_vc_prefix(vc)
            prefix = f"BCS-{extracted}" if extracted else "BCS-CUSTOM"
        elif not prefix.startswith("BCS-"):
            prefix = f"BCS-{prefix}"
    else:
        # 未指定完整 VC，按规则推导前缀并拼接 BCS-{prefix}-{nmId}
        if prefix:
            prefix_clean = prefix.upper().replace("BCS-", "").strip()
            prefix = f"BCS-{prefix_clean}"
        else:
            p_code, _ = resolve_vendor_prefix(cn, matched_map_row.get("goodsCode") if matched_map_row else None, pmap)
            prefix = p_code
        vc = f"{prefix}-{nm_id}"

    # 3. 价格决策
    price = item.price
    if price is None or price <= 0:
        if matched_map_row:
            sp = matched_map_row.get("shop_price")
            dp = matched_map_row.get("dp")
            if sp not in (None, "") and float(sp) > 0:
                price = float(sp)
            elif dp not in (None, "") and float(dp) > 0:
                price = math.floor(float(dp))
        if (price is None or price <= 0) and cn:
            try:
                boss_items = MappingRepository.load_price_book()
                for b_it in boss_items:
                    if b_it.get("cn") == cn and b_it.get("dp") is not None:
                        price = math.floor(float(b_it["dp"]))
                        break
            except Exception:
                pass
    if price is None or price <= 0:
        return None, f"商品 {nm_id} 价格缺失且无法从映射表自动匹配，请通过 --price 指定"

    # 4. 包装尺寸与重量决策
    l, w, h, wt = item.length, item.width, item.height, item.weight
    valid_dims = (
        l is not None and l > 0 and
        w is not None and w > 0 and
        h is not None and h > 0 and
        wt is not None and wt > 0
    )

    card_info = item.extra.get("card_info")
    if card_info:
        card_cache[nm_id] = card_info
    elif nm_id in card_cache:
        card_info = card_cache[nm_id]

    if not valid_dims:
        # 尝试查映射表 / 价格表 / card.json
        dims, err = resolve_replicate_package(vc, cn, nm_id, mapping_state, card_cache)
        if err or not dims:
            # 尝试直接抓 card.json
            if card_info is None:
                card_info = fetch_card_json(nm_id)
                card_cache[nm_id] = card_info
            if card_info:
                pkg = parse_package_info(card_info)
                l = l or pkg.get("length")
                w = w or pkg.get("width")
                h = h or pkg.get("height")
                wt = wt or pkg.get("weight")
            if (not l or not w or not h or not wt) and cn:
                bp = boss_pkg.get(cn)
                if bp:
                    bl, bw, bh, bwt = bp
                    l = l or bl
                    w = w or bw
                    h = h or bh
                    wt = wt or bwt
            if l and w and h and wt:
                l, w, h, wt = math.ceil(float(l)), math.ceil(float(w)), math.ceil(float(h)), round(float(wt), 3)
            else:
                return None, f"商品 {nm_id} 包装尺寸重量缺失（L={l}, W={w}, H={h}, 重量={wt}），请通过 --dims 指定"
        else:
            l, w, h, wt = dims
    else:
        l, w, h, wt = math.ceil(float(l)), math.ceil(float(w)), math.ceil(float(h)), round(float(wt), 3)

    # 5. 标题与图片提取（针对旧接口或详情需要）
    if card_info is None:
        card_info = fetch_card_json(nm_id)
        card_cache[nm_id] = card_info

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
        }
    )
    return resolved, None

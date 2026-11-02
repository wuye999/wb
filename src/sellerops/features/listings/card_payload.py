"""card payload。"""


import copy
import json
import math


from typing import Dict, Any, List, Optional, Tuple
from sellerops.domain.models import PublishItem


from sellerops.features.listings.publish_inputs import stock_for


from sellerops.features.listings.publish_details import build_synthetic_detail

from sellerops.features.listings.publish_details import generate_image_urls


def build_card_payload(
    item: PublishItem,
    target_sids: List[int],
    warehouses: Dict[int, int],
    card_info: Dict[str, Any],
    detail: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, int]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    构造 /system/wbCollection/wb/new 请求体。
    支持任意合法完整 vendorCode。返回 (payload, err)。
    """
    try:
        p_val = float(item.price)
        dims = [float(v) for v in (item.length, item.width, item.height, item.weight)]
        stk = stock_for(item.cn, overrides, default=item.stock)
        if not math.isfinite(p_val) or p_val <= 0:
            raise ValueError("价格必须是正有限数")
        if any(not math.isfinite(v) or v <= 0 for v in dims):
            raise ValueError("包装尺寸和毛重必须是正有限数")
        if isinstance(stk, bool) or not isinstance(stk, int) or stk < 0:
            raise ValueError("库存必须是非负整数")
        if not item.vendor_code or not item.vendor_code.strip():
            raise ValueError("完整 VC 不能为空")
        if not target_sids or len(set(target_sids)) != len(target_sids):
            raise ValueError("目标店铺不能为空或重复")
    except (TypeError, ValueError) as exc:
        return None, f"上架参数无效：{exc}"
    price_str = f"{p_val:.2f}"

    # 1. wbDetail 详情组装与 sizes 重写
    wb_detail = copy.deepcopy(detail) if detail else None
    ci_data = (card_info or {}).get("data") or {}
    subject_id_candidate = (
        item.extra.get("subjectId")
        or item.extra.get("subject_id")
        or ci_data.get("subject_id")
        or (card_info or {}).get("subject_id")
        or (card_info or {}).get("subjectId")
    )
    if not wb_detail:
        wb_detail = build_synthetic_detail(
            card_info=card_info,
            nm_id=item.nm_id,
            title=item.title,
            weight=float(item.weight or 0.0),
            vendor_code=item.vendor_code or "",
            price_str=price_str,
            subject_id=subject_id_candidate,
        )

    sizes = wb_detail.get("sizes") or []
    if not sizes:
        sizes = [{"origName": 0, "name": "", "price": price_str, "vendorCode": item.vendor_code}]
    else:
        # 只保留第一条规格，避免误上兄弟规格
        s0 = sizes[0]
        orig = s0.get("origName")
        if orig is None:
            orig = s0.get("techSize") if s0.get("techSize") else 0
        try:
            orig = int(float(orig))
        except (TypeError, ValueError):
            orig = 0
        sizes = [{
            "origName": orig,
            "name": s0.get("name") if s0.get("name") is not None else (s0.get("wbSize") or ""),
            "price": price_str,
            "vendorCode": item.vendor_code,
        }]
    wb_detail["sizes"] = sizes

    # 2. 类目与层级 ID 健壮解析
    subject_id = wb_detail.get("subjectId") or subject_id_candidate
    if not subject_id:
        return None, "类目 ID 缺失（WB 详情与 card.json 均无 subjectId）"

    parent_id = (
        wb_detail.get("subjectParentId")
        or ci_data.get("subject_root_id")
        or (card_info or {}).get("subject_root_id")
        or (card_info or {}).get("subjectParentId")
        or 0
    )
    parent_name = (card_info or {}).get("subj_root_name") or ""
    subject_name = (card_info or {}).get("subj_name") or wb_detail.get("subjectName") or ""

    # 3. 图片资源健壮补齐（绝不允许提交空字符串）
    images = item.images
    if not images:
        photo_count = ((card_info or {}).get("media") or {}).get("photo_count") or 1
        images = ";".join(generate_image_urls(item.nm_id, photo_count))
    main_image = item.main_image or (images.split(";")[0] if images else "")

    title = item.title or (card_info or {}).get("imt_name") or wb_detail.get("name") or ""

    shop_arr = []
    for sid in target_sids:
        wh_id = warehouses.get(sid)
        if wh_id is None:
            return None, f"店铺 {sid} 缺少对应发货仓库配置"
        shop_arr.append({"id": sid, "warehouseId": wh_id, "warehouseQuantity": stk})

    payload = {
        "shop": json.dumps(shop_arr, ensure_ascii=False, separators=(",", ":")),
        "offerDIY": "",
        "brandStatus": False,
        "oModel": True,
        "status": "1",
        "collectionType": "0",
        "shopDatas": [{
            # ★ nmId 提交前必须置空 null，避免被判定为更新已有卡
            "nmId": None,
            "wbDetail": json.dumps(wb_detail, ensure_ascii=False, separators=(",", ":")),
            "name": title,
            "subjectId": subject_id,
            "parentId": parent_id,
            "wbCardInfo": json.dumps(card_info, ensure_ascii=False, separators=(",", ":")),
            "images": images,
            "video": "",
            "mainImage": main_image,
            "packagLength": math.ceil(float(item.length or 0)),
            "packagWidth": math.ceil(float(item.width or 0)),
            "packagHeight": math.ceil(float(item.height or 0)),
            "weightBrutto": str(item.weight or 0),
            "vendorCode": item.vendor_code,
            "brand": "",
            "hsCode": None,
            "subjectName": subject_name,
            "parentName": parent_name,
            "sourceSku": str(item.nm_id),
        }],
        "mode": 1,
        "mergeCards": 1,
        "carryBrand": 1,
        "customBrand": None,
        "titleSuffix": None,
        "aiRewrite": False,
        "aiRetouch": False,
        "aiRetouchTemplateId": None,
        "imgUploadMode": 0,
    }
    return payload, None

from sellerops.core import utils as common
from sellerops.integrations.wildberries.client import fetch_card_json, card_color_names
from sellerops.persistence import db
from sellerops.persistence.mappings import MappingRepository


def load_context() -> dict:
    entries = MappingRepository.load_vc_entries(MappingRepository.current_products())
    return {vc: db.resolve_vc(vc, entries, include_history=True)
            for vc, row in entries.items() if row['status'] == 'mapped'}

def fetch_product_info(nm_id, vc="", own=None):
    """整合 WB 商品基础信息（标题、品牌、颜色、售价、选项参数等）。供客服与监控模块调用。"""
    info = {"title": "", "brand": "", "colors": "", "price": "", "description": "", "options": ""}
    card = fetch_card_json(nm_id)
    if card:
        info["title"] = card.get("imt_name") or ""
        desc = (card.get("description") or "").strip()
        info["description"] = desc[:400] + ("…" if len(desc) > 400 else "")
        opts_str = "；".join(
            f"{o.get('name')}: {o.get('value')}" for o in (card.get("options") or [])
            if o.get("name") and o.get("value"))
        info["options"] = opts_str[:600] + ("…" if len(opts_str) > 600 else "")
        info["colors"] = card_color_names(card)
    if own is None:
        try:
            own = load_context()
        except Exception:
            own = {}
    row = (own or {}).get(vc or "")
    if row:
        price_v = None
        sp = row.get("shop_price")
        if sp not in (None, ""):
            price_v = float(sp)
        elif row.get("minimum_price") is not None:
            price_v = float(row["minimum_price"])
        if price_v is not None:
            disc = common.to_int(row.get("discount")) if row.get("discount") not in (None, "") else 0
            if disc:
                price_v = price_v * (100 - disc) / 100
            info["price"] = f"{price_v:.0f} CNY"
    return info

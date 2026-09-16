# -*- coding: utf-8 -*-
"""
wb_ops Wildberries CDN 商品卡片 (card.json) 抓取与解析工具
提供 CDN 分片路由、card.json 解析、颜色提取、包装尺寸/重量换算以及商品详情汇总。
"""
import re
import requests
from wb_ops import common
from wb_ops.storage.mapping_repo import MappingRepository

_BASKET_TABLE = [
    (143, "01"), (287, "02"), (431, "03"), (719, "04"), (1007, "05"), (1061, "06"),
    (1115, "07"), (1169, "08"), (1313, "09"), (1601, "10"), (1655, "11"), (1919, "12"),
    (2045, "13"), (2189, "14"), (2405, "15"), (2621, "16"), (2837, "17"), (3053, "18"),
    (3269, "19"), (3485, "20"), (3701, "21"), (3917, "22"), (4133, "23"), (4349, "24"),
    (4565, "25"), (4877, "26"), (5189, "27"), (5501, "28"), (5813, "29"), (6125, "30"),
    (6437, "31"), (6749, "32"), (7061, "33"), (7373, "34"), (7685, "35"), (7997, "36"),
    (8309, "37"), (8741, "38"), (9173, "39"), (9605, "40"), (10373, "41"), (11141, "42"),
    (11909, "43"), (12677, "44"), (13445, "45"), (14213, "46"),
]


def basket_base(nm_id):
    """nmId → basket CDN 基础路径（card.json 基于它）"""
    n = int(nm_id)
    vol, part = n // 100000, n // 1000
    basket = "47"
    for threshold, no in _BASKET_TABLE:
        if vol <= threshold:
            basket = no
            break
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{n}"


def fetch_card_json(nm_id):
    """basket CDN card.json → dict；失败返回 None。供 questions 客服与包装兜底使用。"""
    url = f"{basket_base(nm_id)}/info/ru/card.json"
    try:
        resp = requests.get(url, headers={"User-Agent": common.UA}, timeout=30)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


_own_map_cache = None


def _own_map():
    """价格映射表状态（懒加载缓存）：{vc: {cn, dp, shop_price}}。失败 → {}。"""
    global _own_map_cache
    if _own_map_cache is None:
        try:
            _own_map_cache = MappingRepository.load_mapping_state()[0]
        except Exception:
            _own_map_cache = {}
    return _own_map_cache


def card_color_names(card):
    """从 card.json 提取颜色名。供 questions 模块调用。"""
    colors = card.get("colors") or []
    nm_names = card.get("nm_colors_names")
    if isinstance(nm_names, str):
        nm_names = [nm_names]
    names = []
    for i, item in enumerate(colors):
        if isinstance(item, dict):
            n = item.get("name")
        elif isinstance(nm_names, list) and i < len(nm_names):
            n = nm_names[i]
        else:
            n = None
        if n:
            names.append(n)
    return "、".join(names)


def fetch_product_info(nm_id, vc="", own=None):
    """整合商品信息：供 questions / questions_watch 模块调用。"""
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
    row = (own if own is not None else _own_map()).get(vc or "")
    if row:
        price_v = None
        sp = row.get("shop_price")
        if sp not in (None, ""):
            price_v = float(sp)
        elif row.get("dp") is not None:
            price_v = float(row["dp"])
        if price_v is not None:
            disc = common.to_int(row.get("discount")) if row.get("discount") not in (None, "") else 0
            if disc:
                price_v = price_v * (100 - disc) / 100
            info["price"] = f"{price_v:.0f} CNY"
    return info


def parse_package_info(card_info):
    """card.json grouped_options「Габариты」组 → {'length','width','height','weight'}（仅取包装项）"""
    result = {"length": None, "width": None, "height": None, "weight": None}

    def dim(v):
        t = re.sub(r"\s+", "", str(v or ""))
        if "мм" in t:
            return float(t.split("мм")[0]) / 10
        if "см" in t:
            return float(t.split("см")[0])
        if "м" in t:
            return float(t.split("м")[0]) * 100
        try:
            return float(t)
        except ValueError:
            return None

    def weight(v):
        t = re.sub(r"\s+", "", str(v or ""))
        if "кг" in t:
            return float(t.split("кг")[0])
        if "г" in t:
            return float(t.split("г")[0]) / 1000
        try:
            return float(t)
        except ValueError:
            return None

    for group in (card_info or {}).get("grouped_options") or []:
        if group.get("group_name") != "Габариты":
            continue
        for opt in group.get("options") or []:
            name, val = str(opt.get("name") or ""), opt.get("value")
            if not re.search(r"упаковк", name, re.I):   # 必须是包装尺寸，排除商品本体尺寸
                continue
            if "Длина" in name:
                result["length"] = dim(val)
            elif "Ширина" in name:
                result["width"] = dim(val)
            elif "Высота" in name:
                result["height"] = dim(val)
            elif ("Вес" in name or "Масса" in name) and not re.search(r"без\s*упаковк", name, re.I):
                result["weight"] = weight(val)
    return result

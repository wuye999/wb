# -*- coding: utf-8 -*-
"""
wb_ops Wildberries CDN 商品卡片 (card.json) 抓取与解析工具
提供 CDN 分片路由、card.json 解析、颜色提取、包装尺寸/重量换算以及商品详情汇总。
"""
import math
import random
import re
import string
import requests
from wb_ops import common, config
from wb_ops.storage.mapping_repo import MappingRepository


from wb_ops.adapters.wb_client import (
    _BASKET_TABLE,
    basket_base,
    fetch_card_json,
    card_color_names,
)

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


_pkg_cache = None


def boss_pkg_map():
    """商品价格表「尺寸」列 → {中文名: (L, W, H, weight)}（格式 `长*宽*高/毛重`，容错空格）。
    兜底来源：映射表缺毛重/尺寸时，按中文名取我方商品价格表同名的包装数据。"""
    global _pkg_cache
    if _pkg_cache is not None:
        return _pkg_cache
    import openpyxl
    out = {}
    try:
        wb = openpyxl.load_workbook(config.BOSS_XLSX, data_only=True)
        ws = wb["Sheet1"]
        for r in ws.iter_rows(min_row=2, values_only=True):
            cn = str(r[2] or "").strip()
            dim = str(r[4] or "").strip() if len(r) > 4 else ""
            if not cn or not dim:
                continue
            m = re.match(r"^([\d.]+)\s*\*\s*([\d.]+)\s*\*\s*([\d.]+)\s*/\s*([\d.]+)$", dim)
            if not m:
                continue
            out[cn] = (float(m.group(1)), float(m.group(2)),
                       float(m.group(3)), float(m.group(4)))
        wb.close()
    except Exception:
        pass
    _pkg_cache = out
    return out


def extract_vc_prefix(vc):
    """从现有 vendorCode 提取 4 位前缀码。
    支持：
      BCS-QQNN-1078999444 -> QQNN
      BCS-QQNN-ozon-card-1078999444 -> QQNN
      BCS-QQNN-WRLINWI/1078999444 -> QQNN
    未提取到返回 None
    """
    s = str(vc or "").strip()
    m = re.match(config.VC_PREFIX_RE, s)
    if m:
        return m.group(1).upper()
    m = re.match(r"^BCS-([A-Za-z]{4})(?:-|$|/)", s)
    if m:
        return m.group(1).upper()
    return None


def random_prefix():
    """生成 4 位大写字母随机前缀"""
    return "".join(random.choices(string.ascii_uppercase, k=4))


def resolve_vendor_prefix(cn, old_vc, cn2prefix=None):
    """根据规则匹配并生成提交给新批量上品接口的 vendorCodePrefix（必须携带 BCS- 前缀）。"""
    prefix = None
    source = "随机"
    if cn and cn2prefix and cn in cn2prefix:
        p = str(cn2prefix[cn]).strip().upper()
        if p:
            prefix = p
            source = "价格表"
    if not prefix and old_vc:
        p = extract_vc_prefix(old_vc)
        if p:
            prefix = p
            source = "源vc"
    if not prefix:
        prefix = random_prefix()
        source = "随机"

    if not prefix.startswith("BCS-"):
        prefix = f"BCS-{prefix}"
    return prefix, source


def resolve_replicate_package(vc, cn, nm_id, map_state, card_cache=None):
    """
    获取复制上架的包装尺寸与重量：
    ★ 严格规则：绝不使用快照的尺寸数据！
    1. 优先读取价格映射表 (map_state.get(vc)) 的 L, W, H, weight
    2. 若缺失或 <= 0，查商品价格表 (boss_pkg_map) 兜底
    3. 若仍缺失，查 card.json 兜底
    返回: ((L, W, H, weight), None) 或 (None, err_msg)
    其中 L, W, H 为整型 cm (ceil)，weight 为 float kg (round 3)
    """
    row = (map_state or {}).get(vc) or {}
    length = row.get("L")
    width = row.get("W")
    height = row.get("H")
    weight = row.get("weight")

    def _valid(val):
        try:
            return float(val) > 0
        except (TypeError, ValueError):
            return False

    # 2. 商品价格表兜底
    if not (_valid(length) and _valid(width) and _valid(height) and _valid(weight)):
        bp = boss_pkg_map().get(cn or "")
        if bp:
            bl, bw, bh, bwgt = bp
            if not _valid(length):
                length = bl
            if not _valid(width):
                width = bw
            if not _valid(height):
                height = bh
            if not _valid(weight):
                weight = bwgt

    # 3. card.json 兜底
    if not (_valid(length) and _valid(width) and _valid(height) and _valid(weight)):
        card_info = None
        if card_cache is not None and nm_id in card_cache:
            card_info = card_cache[nm_id]
        else:
            card_info = fetch_card_json(nm_id)
            if card_cache is not None:
                card_cache[nm_id] = card_info
        if card_info:
            pkg = parse_package_info(card_info)
            if not _valid(length):
                length = pkg.get("length")
            if not _valid(width):
                width = pkg.get("width")
            if not _valid(height):
                height = pkg.get("height")
            if not _valid(weight):
                weight = pkg.get("weight")

    if not (_valid(length) and _valid(width) and _valid(height) and _valid(weight)):
        return None, f"包装数据缺失（L={length} W={width} H={height} 重={weight}）"

    return (
        math.ceil(float(length)),
        math.ceil(float(width)),
        math.ceil(float(height)),
        round(float(weight), 3)
    ), None


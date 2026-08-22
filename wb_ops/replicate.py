# -*- coding: utf-8 -*-
"""
wb_ops 跨店复制上架（replicate）

把只覆盖部分店铺的商品（按 vendorCode 判断）上架到缺失的店铺：
- 覆盖判断：5 店快照（filter=BASE，仅滤 trashedAt）按 vendorCode 汇总 → 缺失店即目标店。
- 上架提交：POST /system/wbCollection/wb/new（API 文档 §2.6），vendorCode 与源店完全一致。
- 商品数据：源店快照（标题/图片/尺寸/价格）+ WB detail（BCS 代理）+ card.json（basket CDN）。
- 防重复：快照覆盖判断 + 执行时 vendorCodeMulti(filter=ALL) 实时查重，双层防护。

用法：wb.py replicate [--vc|--prefix|--name|--shops|--limit] [--apply] [--no-verify] [--interval S]
"""
import copy
import csv
import json
import math
import os
import re
import time
from datetime import datetime

import requests

from . import bcs
from . import common
from . import config
from . import products

# ---------------- 常量 ----------------
DETAIL_URL_TPL = ("https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
                  "?appType=1&curr=rub&dest=-1257786&spp=30&hide_vflags=4294967296"
                  "&hide_dtype=15&mtype=257&lang=ru&ab_testing=false&nm={nm}")
# basket 分片表（来源：批量上架项目 45-api-wb.js 权威表，46 区间，勿改动）
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
DETAIL_INTERVAL = 1.2      # detail 请求间隔（秒）
CARD_INTERVAL = 0.5        # card.json 请求间隔（秒）
DETAIL_FAIL_ABORT = 3      # 连续 N 个 vc detail 失败 → 中止（防反爬雪崩）
DEFAULT_STOCK = 999        # 上架默认库存（对齐批量上架脚本 A6 设计）
RE_REGION = re.compile(r"подольск|электросталь|хоругвино|колчанино|восток|север|юг", re.I)


def parse_cn_stock(s):
    """解析 `--cn-stock` 参数："中文名:库存,中文名:库存" → {中文名: 库存}。空/非法项忽略。"""
    out = {}
    for part in (s or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        cn, v = part.split(":", 1)
        cn = cn.strip()
        try:
            out[cn] = int(str(v).strip())
        except ValueError:
            print(f"  [警告] --cn-stock 非法库存值，已忽略：{part}")
    return out


def stock_for(cn, overrides=None):
    """上架库存：overrides（--cn-stock 指定）优先 → 默认 999"""
    overrides = overrides or {}
    if cn in overrides:
        return overrides[cn]
    return DEFAULT_STOCK


def _basket_base(nm_id):
    """nmId → basket CDN 基础路径（vol/part/图片与 card.json 都基于它）"""
    n = int(nm_id)
    vol, part = n // 100000, n // 1000
    basket = "47"
    for threshold, no in _BASKET_TABLE:
        if vol <= threshold:
            basket = no
            break
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{n}"


# ---------------- 覆盖计算 ----------------
def build_coverage(shops_data):
    """读全部店铺快照（仅滤 trashedAt，不滤无价格）→ 覆盖状态。
    shops_data: products.load_all_shops() 的 {店id: rows}。
    返回 (vc_shops, vc_rows, shop_ids)：
      vc_shops: {vc: set(店id)}   vc_rows: {vc: {店id: row}}   shop_ids: [全部店id]"""
    vc_shops, vc_rows = {}, {}
    shop_ids = sorted(shops_data.keys())
    for sid, rows in shops_data.items():
        for r in rows:
            vc = r.get("vendorCode")
            if not vc:
                continue
            vc_shops.setdefault(vc, set()).add(sid)
            vc_rows.setdefault(vc, {})[sid] = r
    return vc_shops, vc_rows, shop_ids


def pick_source(vc, vc_rows, sid_main):
    """源店选择：有该 vc 且 sizeList[0].price 非空的店，主店优先，否则最小 sid。
    返回 (src_sid, src_row) 或 (None, None)（全店无价格，不可作源）。"""
    candidates = []
    for sid, r in vc_rows.get(vc, {}).items():
        sl = r.get("sizeList") or []
        if sl and sl[0].get("price") is not None:
            candidates.append((sid, r))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: (0 if x[0] == sid_main else 1, x[0]))
    return candidates[0]


# ---------------- 目标仓库 ----------------
_warehouse_cache = {}
# 最后兜底：当前账号 5 店实测主仓库（2026-08-20；快照与仓库 API 均失效时使用，换账号需更新）
KNOWN_WAREHOUSES = {5272: 1947728, 5273: 1947984, 5276: 1948249, 5280: 1948377, 5281: 1948455}


def main_warehouse(sid, shops_data=None):
    """店铺主仓库：默认仓库（莫斯科，config.DEFAULT_WAREHOUSE_NAME）→ 俄罗斯地区仓 → KNOWN_WAREHOUSES 兜底。
    成都仓库（国内仓，name="成都仓库"）不参与选择。失败返回 None。"""
    if sid in _warehouse_cache:
        return _warehouse_cache[sid]
    # 1. 默认仓库（莫斯科）
    wh_id = bcs.default_warehouse_id(sid)
    # 2. 俄罗斯其他地区仓兜底（莫斯科之外，如 подольск 等）
    if wh_id is None:
        whs = bcs.fetch_warehouses(sid)
        wh_id = next((w["id"] for w in whs if RE_REGION.search(w.get("name") or "")), None)
    # 3. 硬编码兜底（仓库 API 空返回时）
    if wh_id is None:
        wh_id = KNOWN_WAREHOUSES.get(sid)
    _warehouse_cache[sid] = wh_id
    return wh_id


# ---------------- WB 数据获取 ----------------
def _fetch_wb_detail_bcs(nm_id):
    """WB detail（BCS 代理，带 BCS 凭证）→ product 对象；失败返回 None。"""
    from urllib.parse import quote
    wb_url = DETAIL_URL_TPL.format(nm=nm_id)
    url = f"{bcs.base_url()}/wb/api/proxy/common?url={quote(wb_url, safe='')}"
    try:
        d = bcs.http_get_json(url)
        if d.get("code") != 200:
            return None
        products_ = (d.get("data") or {}).get("products") or []
        return products_[0] if products_ else None
    except Exception:
        return None


def fetch_wb_detail(nm_id):
    """WB detail（BCS 代理）→ product 对象；失败返回 None。
    （synthetic 拼装通道由 run() 直接调用 build_synthetic_detail）"""
    return _fetch_wb_detail_bcs(nm_id)


def fetch_card_json(nm_id):
    """basket CDN card.json → dict；失败返回 None。"""
    url = f"{_basket_base(nm_id)}/info/ru/card.json"
    try:
        resp = requests.get(url, headers={"User-Agent": common.UA}, timeout=30)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def build_synthetic_detail(meta, card_info, nm_id):
    """用 BCS 商品数据 + card.json 拼装伪 detail product（替代 WB detail）。

    背景：BCS 代理 detail 持续超时；而 BCS 列表行 / 他人映射表行 + card.json
    （CDN 直连）已覆盖 wbDetail 所需字段的绝大部分。
    已实验验证（2026-08-22）：拼装上架 BCS-YSGK-1317303667 → 店5281 成功建卡，
    商品信息（标题/图片11/尺寸/价格）与源店一致，同步后 vendorCode 可查。
    meta：BCS list 行（replicate 场景，键 title/sizeList/dimensionsWeightBrutto）
    或他人表 item（import-shelve 场景，键 title_ru/weight，subjectId 由 card.json 兜底）。
    返回伪 detail product dict；sizes 仅非空占位（build_payload 会重写）。"""
    ci = card_info or {}
    data = ci.get("data") or {}
    sizes = []
    for s in (meta.get("sizeList") or []):
        orig = s.get("techSizeName")
        try:
            orig = int(float(orig)) if orig not in (None, "") else 0
        except (TypeError, ValueError):
            orig = 0
        sizes.append({"origName": orig, "name": str(s.get("techSizeName") or ""),
                      "price": s.get("price"), "vendorCode": meta.get("vendorCode")})
    if not sizes:  # 占位（build_payload/build_import_payload 会重写为正确格式）
        sizes = [{"origName": 0, "name": "", "price": None, "vendorCode": meta.get("vendorCode")}]
    return {
        "id": int(nm_id),
        "root": ci.get("imt_id") or meta.get("imtId") or 0,
        "brand": meta.get("brand") or "",
        "brandId": 0,
        "colors": ci.get("colors") or [],
        "subjectId": meta.get("subjectId") or data.get("subject_id"),
        "subjectParentId": data.get("subject_root_id") or 0,
        "name": meta.get("title") or meta.get("title_ru") or ci.get("imt_name") or "",
        "pics": (ci.get("media") or {}).get("photo_count") or 0,
        "weight": meta.get("dimensionsWeightBrutto") or meta.get("weight") or 0,
        "sizes": sizes,
    }


_own_map_cache = None


def _own_map():
    """价格映射表状态（懒加载缓存）：{vc: {cn, dp, shop_price}}。失败 → {}。"""
    global _own_map_cache
    if _own_map_cache is None:
        try:
            from . import mapping
            _own_map_cache = mapping.load_mapping_state()[0]
        except Exception:
            _own_map_cache = {}
    return _own_map_cache


def _card_color_names(card):
    """从 card.json 提取颜色名（兼容 colors 为 dict 列表 / ID 列表 + nm_colors_names 兜底）。
    card.json 通常只有颜色 ID（nm_colors_names 常为 null），拿不到名字时返回空串。"""
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
    """整合商品信息：card.json（标题+描述+特征+颜色）+ 价格映射表行（店铺价CNY）。
    返回 dict {title, brand, colors, price, description, options}；拉不到的字段为空。
    描述/特征截断，避免 token 过载。
    ★ 不再依赖 WB detail（BCS 代理超时易失败）：brand 无来源留空；价格=价格映射表店铺价(CNY)。
    own: 价格映射表状态 {vc: {cn,dp,shop_price}}（None 时内部懒加载）。"""
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
        info["colors"] = _card_color_names(card)
    # 价格：价格映射表店铺价(CNY，原价) 减去折扣% = 实际售价；无折扣列时按原价
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
                price_v = price_v * (100 - disc) / 100  # WB 折扣=减价%，现价=原价×(1-disc/100)
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


def generate_image_urls(nm_id, photo_count):
    """按 photo_count 生成 basket big 图 URL 列表（无图兜底单张主图）"""
    urls = [f"{_basket_base(nm_id)}/images/big/{i}.webp" for i in range(1, (photo_count or 0) + 1)]
    if not urls:
        urls = [f"{_basket_base(nm_id)}/images/big/1.webp"]
    return urls


# ---------------- 查重（本地记录 + 实时 API 双防线） ----------------
RECORDS_JSON = os.path.join(config.STATE_DIR, "复制上架记录.json")


def _load_records():
    """本地提交记录 {vc: {店id字符串: 提交时间}}——防 BCS 缓存滞后窗口内重复提交"""
    try:
        return json.load(open(RECORDS_JSON, encoding="utf-8"))
    except Exception:
        return {}


def _save_records(records):
    os.makedirs(os.path.dirname(RECORDS_JSON), exist_ok=True)
    with open(RECORDS_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def vc_exists_in_shop(vc, sid, records=None):
    """执行时查重：先查本地提交记录（BCS 缓存滞后窗口内 API 不可靠），再查 API（filter=ALL 含草稿箱/回收站）"""
    if records and str(sid) in (records.get(vc) or {}):
        return True
    url = (f"{bcs.base_url()}/shopKeeper/productList/list?filter=ALL&pageNum=1&pageSize=10"
           f"&shopId={sid}&vendorCodeMulti={vc}")
    try:
        d = bcs.http_get_json(url)
        return bool(d.get("code") == 200 and d.get("rows"))
    except Exception:
        return False  # 查询失败按不存在处理，由 BCS 服务端 vendorCode 幂等兜底（实测重复提交不建重复商品）


# ---------------- 上架请求体构造 ----------------
def build_payload(vc, src_sid, src_row, target_sids, warehouses, detail, card_info, cn="-", overrides=None):
    """构造 /system/wbCollection/wb/new 请求体。返回 (payload, err)。
    vendorCode 与源店完全一致；价格=源店现价；直上/不合并/不带品牌。
    cn/overrides：用于按中文名决定上架库存（stock_for）。"""
    nm_id = int(vc.rsplit("-", 1)[-1])
    sl = src_row.get("sizeList") or []
    price = sl[0].get("price") if sl else None
    if price is None:
        return None, "源店无价格"
    price_str = f"{float(price):.2f}"

    # wbDetail：detail product 深拷贝 + sizes 重写（对齐源店规格数，绝大多数 1 条）
    wb_detail = copy.deepcopy(detail)
    sizes = wb_detail.get("sizes") or []
    if not sizes:
        return None, "WB 详情缺少规格数据（sizes 为空）"
    n = max(len(sl), 1)
    new_sizes = []
    for s in sizes[:n]:
        orig = s.get("origName")
        if orig is None:
            orig = s.get("techSize") if s.get("techSize") else 0
        try:
            orig = int(float(orig))
        except (TypeError, ValueError):
            orig = 0
        new_sizes.append({"origName": orig,
                          "name": s.get("name") if s.get("name") is not None else (s.get("wbSize") or ""),
                          "price": price_str,
                          "vendorCode": vc})
    wb_detail["sizes"] = new_sizes

    # 图片：源店快照优先，缺失按 photo_count 生成
    images = src_row.get("images") or ""
    if not images:
        images = ";".join(generate_image_urls(nm_id, ((card_info or {}).get("media") or {}).get("photo_count")))
    main_image = src_row.get("repImg") or images.split(";")[0]

    # 包装尺寸/重量：源店快照优先 → card.json 包装组兜底 → 仍缺则失败（不造假数据）
    pkg = parse_package_info(card_info)
    length = src_row.get("dimensionsLength") or pkg["length"]
    width = src_row.get("dimensionsWidth") or pkg["width"]
    height = src_row.get("dimensionsHeight") or pkg["height"]
    weight = src_row.get("dimensionsWeightBrutto") or pkg["weight"]
    if not length or not width or not height or not weight:
        return None, f"包装数据缺失（L={length} W={width} H={height} 重={weight}）"

    subject_id = src_row.get("subjectId") or detail.get("subjectId")
    if not subject_id:
        return None, "类目 ID 缺失"
    parent_id = detail.get("subjectParentId") or ((card_info or {}).get("data") or {}).get("subject_root_id") or 0
    parent_name = (card_info or {}).get("subj_root_name") or ""

    shop_arr = [{"id": sid, "warehouseId": warehouses[sid], "warehouseQuantity": stock_for(cn, overrides)}
                for sid in target_sids]
    payload = {
        "shop": json.dumps(shop_arr, ensure_ascii=False, separators=(",", ":")),
        "offerDIY": "",
        "brandStatus": False,
        "oModel": True,
        "status": "1",
        "collectionType": "0",
        "shopDatas": [{
            "nmId": nm_id,
            "wbDetail": json.dumps(wb_detail, ensure_ascii=False, separators=(",", ":")),
            "name": src_row.get("title") or "",
            "subjectId": subject_id,
            "parentId": parent_id,
            "wbCardInfo": json.dumps(card_info, ensure_ascii=False, separators=(",", ":")),
            "images": images,
            "video": src_row.get("video") or "",
            "mainImage": main_image,
            "packagLength": math.ceil(float(length)),
            "packagWidth": math.ceil(float(width)),
            "packagHeight": math.ceil(float(height)),
            "weightBrutto": str(weight),
            "vendorCode": vc,
            "brand": "",
            "hsCode": None,
            "subjectName": src_row.get("subjectName") or "",
            "parentName": parent_name,
            "sourceSku": str(nm_id),
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


# ---------------- 主流程 ----------------
def ensure_snapshots(args):
    """前置自动同步：启动时先刷新全部店铺快照（WB→BCS 并发同步 + 逐店拉取，约 2 分钟），
    保证覆盖/差集判断基于最新数据（不做时效校验，直接同步最新）。
    --no-sync 可跳过（用本地快照，判断可能滞后）。"""
    if getattr(args, "no_sync", False):
        print("[前置同步] --no-sync 已指定，跳过同步，直接使用本地快照（覆盖判断可能滞后）")
        return
    print("[前置同步] 刷新全部店铺快照（WB→BCS 同步 + 拉取，约 2 分钟）...")
    products.fetch_all()


def run(args):
    from . import mapping  # 延迟 import（mapping → bcs 联网懒加载）
    overrides = parse_cn_stock(getattr(args, "cn_stock", "") or "")
    ensure_snapshots(args)
    shops_data, _ = products.load_all_shops()
    vc_shops, vc_rows, all_shop_ids = build_coverage(shops_data)
    sid_main = mapping.shop_id()

    # 中文名（映射表有则显示）
    cn_map = {}
    try:
        state, _ = mapping.load_mapping_state()
        cn_map = {vc: v.get("cn") or "" for vc, v in state.items()}
    except Exception:
        pass

    # 目标店限定
    allow_shops = ([int(x) for x in args.shops.split(",") if x.strip()]
                   if getattr(args, "shops", "") else None)

    # 候选：部分覆盖的 vc（1 <= 覆盖店数 < 全店数）
    partial = []
    for vc, sids in sorted(vc_shops.items()):
        missing = [sid for sid in all_shop_ids if sid not in sids]
        if not missing:
            continue
        if allow_shops:
            missing = [sid for sid in missing if sid in allow_shops]
            if not missing:
                continue
        src_sid, src_row = pick_source(vc, vc_rows, sid_main)
        partial.append((vc, src_sid, src_row, missing))

    # 筛选
    if getattr(args, "vc", ""):
        wanted = {x.strip() for x in args.vc.split(",") if x.strip()}
        partial = [p for p in partial if p[0] in wanted]
    if getattr(args, "prefix", ""):
        partial = [p for p in partial
                   if (re.match(config.VC_PREFIX_RE, p[0]) and
                       re.match(config.VC_PREFIX_RE, p[0]).group(1) == args.prefix.upper())]
    if getattr(args, "name", ""):
        kw = args.name
        partial = [p for p in partial if kw in (cn_map.get(p[0]) or "")]
    if getattr(args, "limit", 0):
        partial = partial[:args.limit]

    no_source = [p for p in partial if p[1] is None]
    plans = [p for p in partial if p[1] is not None]

    print(f"店铺: {all_shop_ids}（主店 {sid_main}）")
    print(f"候选 {len(partial)} 个 vc（部分覆盖），其中 {len(no_source)} 个全店无价格（不可上架），{len(plans)} 个可执行")
    if no_source:
        print("[无可用源清单] " + ", ".join(p[0] for p in no_source[:20]) + ("..." if len(no_source) > 20 else ""))

    # dry-run 清单
    print("\n" + "=" * 80)
    print(f"{'vendorCode':<28} {'中文名':<12} {'源店':<6} {'价格':<8} 目标店")
    print("-" * 80)
    for vc, src_sid, src_row, missing in plans:
        price = (src_row.get("sizeList") or [{}])[0].get("price")
        cn = cn_map.get(vc) or "-"
        print(f"{vc:<28} {cn:<12} {src_sid:<6} {price:<8} {','.join(str(s) for s in missing)}")
    print("=" * 80)

    if not plans:
        print("无可执行任务")
        return 0

    if not args.apply:
        print(f"\n[dry-run] 共 {len(plans)} 个 vc 待补齐。加 --apply 执行。")
        return 0

    # ---- 执行 ----
    print(f"\n开始执行：{len(plans)} 个 vc ...")
    # 仓库解析（每店一次）
    warehouses = {}
    for sid in all_shop_ids:
        wh = main_warehouse(sid, shops_data)
        if wh is None:
            print(f"  [警告] 店 {sid} 无法解析主仓库，涉及该店的目标将跳过")
        else:
            warehouses[sid] = wh
    print(f"各店主仓库: {warehouses}")

    records = _load_records()

    csv_path = os.path.join(config.LOG_DIR, f"复制上架_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(config.LOG_DIR, exist_ok=True)
    csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(csv_file)
    writer.writerow(["时间", "vendorCode", "中文名", "源店", "目标店", "价格", "结果", "原因"])

    ok = skip = fail = 0
    detail_fail_streak = 0
    aborted = False
    t0 = time.time()
    for i, (vc, src_sid, src_row, missing) in enumerate(plans, 1):
        tag = f"[{i}/{len(plans)}]"
        cn = cn_map.get(vc) or "-"
        price = (src_row.get("sizeList") or [{}])[0].get("price")
        now = datetime.now().strftime("%H:%M:%S")
        if aborted:
            writer.writerow([now, vc, cn, src_sid, ",".join(map(str, missing)), price, "中止", "连续 detail 失败触发中止"])
            skip += 1
            continue

        # 1) 查重：本地记录 + 实时 API，剔除已存在店
        targets = []
        for sid in missing:
            if sid not in warehouses:
                continue
            if vc_exists_in_shop(vc, sid, records):
                print(f"  {tag} [跳过] 店{sid} 已存在 {vc}")
            else:
                targets.append(sid)
        if not targets:
            print(f"  {tag} [跳过] {vc} 目标店均已存在或无仓库")
            writer.writerow([now, vc, cn, src_sid, ",".join(map(str, missing)), price, "跳过", "已存在或无仓库"])
            skip += 1
            continue

        # 2) WB detail：按 --detail-source 选通道
        #    synthetic=BCS 列表 + card.json 拼装（不依赖 WB detail）
        #    auto=BCS 代理，失败后拼装兜底；bcs=仅 BCS 代理
        nm_id = vc.rsplit("-", 1)[-1]
        ds = getattr(args, "detail_source", "auto")
        card_info = None
        if ds == "synthetic":
            card_info = fetch_card_json(nm_id)
            time.sleep(CARD_INTERVAL)
            detail = build_synthetic_detail(src_row, card_info, nm_id) if card_info else None
        else:
            detail = fetch_wb_detail(nm_id)
            time.sleep(DETAIL_INTERVAL)
            if detail is None and ds == "auto":  # auto 兜底：BCS 拼装
                card_info = fetch_card_json(nm_id)
                time.sleep(CARD_INTERVAL)
                if card_info is not None:
                    detail = build_synthetic_detail(src_row, card_info, nm_id)
        if detail is None:
            # ★ synthetic 模式失败=该商品 card.json 缺失（个体坏数据），非反爬限流，不累计中止计数
            if ds != "synthetic":
                detail_fail_streak += 1
                if detail_fail_streak >= DETAIL_FAIL_ABORT:
                    print("  [中止] 连续多个商品 detail 失败，疑似反爬限流，停止后续执行（已成功不回滚）")
                    aborted = True
            print(f"  {tag} [失败] {vc} WB detail 获取失败")
            writer.writerow([now, vc, cn, src_sid, ",".join(map(str, targets)), price, "失败", "WB detail 获取失败"])
            fail += 1
            continue
        detail_fail_streak = 0

        # 3) card.json（CDN 直连；synthetic/auto 兜底已取则跳过）
        if card_info is None:
            card_info = fetch_card_json(nm_id)
            time.sleep(CARD_INTERVAL)
        if card_info is None:
            print(f"  {tag} [失败] {vc} card.json 获取失败")
            writer.writerow([now, vc, cn, src_sid, ",".join(map(str, targets)), price, "失败", "card.json 获取失败"])
            fail += 1
            continue

        # 4) 构造 + 逐店提交
        # ★ BCS 平台 bug（2026-08-20 前后）：shop 数组带多店一次提交返回 200 但静默不生效
        #   （4 店对照实验：一次提交全部未创建；逐店提交立即生效）。后续 BCS 修正后可恢复
        #   多店一次提交（提效），恢复前先用小批量多店提交验证 vendorCode 确实创建。
        for sid in targets:
            payload, err = build_payload(vc, src_sid, src_row, [sid], warehouses, detail, card_info, cn, overrides)
            if payload is None:
                fail += 1
                print(f"  {tag} [失败] {vc} 店{sid}: {err}")
                writer.writerow([now, vc, cn, src_sid, str(sid), price, "失败", err])
                continue
            try:
                resp = bcs.http_post_json(f"{bcs.base_url()}/system/wbCollection/wb/new", payload)
                if resp.get("code") == 200:
                    ok += 1
                    print(f"  {tag} [成功] {vc} → 店{sid}（¥{price}）")
                    writer.writerow([now, vc, cn, src_sid, str(sid), price, "成功", ""])
                    # 本地记录：防缓存滞后窗口内重复提交
                    records.setdefault(vc, {})[str(sid)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _save_records(records)
                else:
                    fail += 1
                    msg = resp.get("msg") or f"code={resp.get('code')}"
                    print(f"  {tag} [失败] {vc} 店{sid}: {msg}")
                    writer.writerow([now, vc, cn, src_sid, str(sid), price, "失败", msg])
            except Exception as e:
                fail += 1
                print(f"  {tag} [失败] {vc} 店{sid}: {e}")
                writer.writerow([now, vc, cn, src_sid, str(sid), price, "失败", str(e)])
            time.sleep(args.interval)
        if i < len(plans):
            time.sleep(args.interval)

    csv_file.close()
    print(f"\n[汇总] 计划 {len(plans)} | 成功 {ok} | 跳过 {skip} | 失败 {fail}（{time.time() - t0:.0f}s）")
    print(f"明细：{csv_path}")

    # ---- 写后验证：fetch 同步复核覆盖率 ----
    if ok > 0 and not args.no_verify:
        print("\n[写后验证] 触发全店同步 + 拉取（~1.5 分钟）...")
        try:
            products.fetch_all()
            shops_data2, _ = products.load_all_shops()
            vc_shops2, _, _ = build_coverage(shops_data2)
            before_full = sum(1 for s in vc_shops.values() if len(s) == len(all_shop_ids))
            after_full = sum(1 for s in vc_shops2.values() if len(s) == len(all_shop_ids))
            per_shop = {sid: len([1 for s in vc_shops2.values() if sid in s]) for sid in all_shop_ids}
            print(f"[验证] 全店覆盖 vc 数：{before_full} → {after_full}")
            print(f"[验证] 各店在架 vc 数：{per_shop}")
        except Exception as e:
            print(f"[验证] 失败：{e}（可稍后手动 wb.py fetch 复核）")
    return 0

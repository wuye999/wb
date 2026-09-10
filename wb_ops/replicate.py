# -*- coding: utf-8 -*-
"""
wb_ops 跨店复制上架（replicate）

把只覆盖部分店铺的商品（按 vendorCode 判断）上架到缺失的店铺：
- 覆盖判断：多店快照（filter=BASE，仅滤 trashedAt）按 vendorCode 汇总 → 缺失店即目标店。
- 上架提交：POST /products/batch/push（BCS 新版批量上品接口），单批次最多 50 个商品批量推送。
- 商品数据：
  * sku: 必须读取映射表「WB商品码」列（商品真实 WB 编号），无商品码坚决跳过；
  * 包装尺寸：优先读取映射表（尺寸长/宽/高/毛重，绝不读取快照尺寸）→ 商品价格表兜底 → card.json 兜底；
  * 前缀码：中文名匹配商品价格表前缀码优先 → 提取原 vendorCode 前缀码 → 随机 4 位大写字母（均自动拼接 BCS- 前缀）。
- 防重复：快照覆盖判断 + 本地记录 + 执行时 vendorCodeMulti(filter=ALL) 实时查重。

用法：wb.py replicate [--vc|--prefix|--name|--shops|--limit] [--apply] [--no-verify] [--interval S]
"""
import copy
import csv
import json
import math
import os
import random
import re
import string
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

# basket 分片表（来源：批量上架项目 45-api-wb.js 权威表，46 区间，供 card.json 包装与商品详情查询使用）
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
CARD_INTERVAL = 0.5        # card.json 请求间隔（秒）
DEFAULT_STOCK = 999        # 上架默认库存
BATCH_SIZE = 50            # 新版批量上品单次最大商品数
RE_REGION = re.compile(r"подольск|электросталь|хоругвино|колчанино|восток|север|юг", re.I)


def fetch_wb_detail(nm_id):
    """[已弃用] 旧版 WB detail 抓取桩函数（新版批量上品接口由 BCS 后端自动抓取，已无需本地抓取）"""
    return None


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
    """nmId → basket CDN 基础路径（card.json 基于它）"""
    n = int(nm_id)
    vol, part = n // 100000, n // 1000
    basket = "47"
    for threshold, no in _BASKET_TABLE:
        if vol <= threshold:
            basket = no
            break
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{n}"


# ---------------- 商品价格表尺寸缓存 ----------------
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


# ---------------- 供应商代码前缀处理 ----------------
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
    """根据规则匹配并生成提交给新批量上品接口的 vendorCodePrefix（必须携带 BCS- 前缀）。
    规则：
    1. 能识别中文名 → 匹配商品价格表前缀码，拼接为 BCS-{前缀码}
    2. 匹配不到 → 尝试从旧 vendorCode 提取 4 位前缀码，拼接为 BCS-{前缀码}
    3. 仍没有 → 随机生成 4 位大写字母，拼接为 BCS-{4位字母}
    返回: (prefix_with_bcs, prefix_source)
    """
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


# ---------------- card.json 与包装数据获取 ----------------
def fetch_card_json(nm_id):
    """basket CDN card.json → dict；失败返回 None。供 questions 客服与包装兜底使用。"""
    url = f"{_basket_base(nm_id)}/info/ru/card.json"
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
            from . import mapping
            _own_map_cache = mapping.load_mapping_state()[0]
        except Exception:
            _own_map_cache = {}
    return _own_map_cache


def _card_color_names(card):
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
        info["colors"] = _card_color_names(card)
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
    """执行时查重：先查本地提交记录，再查 API（filter=ALL 含草稿箱/回收站）"""
    if records and str(sid) in (records.get(vc) or {}):
        return True
    url = (f"{bcs.base_url()}/shopKeeper/productList/list?filter=ALL&pageNum=1&pageSize=10"
           f"&shopId={sid}&vendorCodeMulti={vc}")
    try:
        d = bcs.http_get_json(url)
        return bool(d.get("code") == 200 and d.get("rows"))
    except Exception:
        return False


# ---------------- 主流程 ----------------
def ensure_snapshots(args):
    """前置同步：启动时刷新全部店铺快照"""
    if not getattr(args, "sync", False):
        print("[前置同步] 未加 --sync，跳过同步，使用本地快照（覆盖/差集判断可能滞后；需最新请加 --sync）")
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

    # 映射表状态：中文名 + 可靠 WB商品码（映射表） + 映射表包装尺寸
    mapping_state, _ = mapping.load_mapping_state()
    cn_map = {vc: v.get("cn") or "" for vc, v in mapping_state.items()}
    own_nm = {vc: str(v.get("nmId") or "").strip() for vc, v in mapping_state.items()}

    # 商品价格表前缀映射：{中文名: 前缀码}
    pmap = mapping.load_prefix_map()
    cn2prefix = {}
    for prefix, info in pmap.items():
        cn2prefix.setdefault(info.get("cn") or "", prefix)

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

    # 校验：必须读取价格映射表的「WB商品码」这一列的码，绝不能从 vendorCode 提取末尾数字；无商品码坚决跳过
    no_nm = [p for p in plans if not (own_nm.get(p[0]) and own_nm.get(p[0]).isdigit())]

    print(f"店铺: {all_shop_ids}（主店 {sid_main}）")
    print(f"候选 {len(partial)} 个 vc（部分覆盖），其中 {len(no_source)} 个全店无价格（不可上架），"
          f"{len(no_nm)} 个映射表无有效WB商品码（跳过避免误上架），{len(plans) - len(no_nm)} 个可执行")
    if no_source:
        print("[无可用源清单] " + ", ".join(p[0] for p in no_source[:20]) + ("..." if len(no_source) > 20 else ""))
    if no_nm:
        print("[无WB商品码跳过] " + ", ".join(p[0] for p in no_nm[:20]) + ("..." if len(no_nm) > 20 else ""))

    # dry-run 清单
    print("\n" + "=" * 100)
    print(f"{'vendorCode':<28} {'中文名':<14} {'源店':<6} {'价格':<8} {'WB商品码':<12} {'提交前缀':<12} 目标店")
    print("-" * 100)
    for vc, src_sid, src_row, missing in plans:
        sl = src_row.get("sizeList") or []
        price = sl[0].get("price") if sl else None
        if price is None and vc in mapping_state:
            price = mapping_state[vc].get("shop_price") or mapping_state[vc].get("dp")
        cn = cn_map.get(vc) or "-"
        rel_nm = own_nm.get(vc) or ""
        valid_nm = rel_nm if rel_nm.isdigit() else ""
        target_prefix, prefix_src = resolve_vendor_prefix(cn, vc, cn2prefix)
        flag = "  [跳过-无有效WB商品码]" if not valid_nm else ""
        print(f"{vc:<28} {cn:<14} {src_sid:<6} {str(price):<8} {valid_nm:<12} {target_prefix:<12} {','.join(str(s) for s in missing)}{flag}")
    print("=" * 100)

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
    card_cache = {}

    csv_path = os.path.join(config.LOG_DIR, f"复制上架_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(config.LOG_DIR, exist_ok=True)
    csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(csv_file)
    writer.writerow(["时间", "vendorCode", "中文名", "WB商品码", "源店", "目标店", "提交前缀", "价格", "结果", "原因"])

    ok = skip = fail = 0
    t0 = time.time()

    # 1) 逐个商品进行前置校验与数据准备
    valid_items = []
    for i, (vc, src_sid, src_row, missing) in enumerate(plans, 1):
        tag = f"[{i}/{len(plans)}]"
        cn = cn_map.get(vc) or "-"
        sl = src_row.get("sizeList") or []
        price = sl[0].get("price") if sl else None
        if price is None and vc in mapping_state:
            price = mapping_state[vc].get("shop_price") or mapping_state[vc].get("dp")
        now = datetime.now().strftime("%H:%M:%S")

        # 0) 严格 WB商品码校验：必须存在且纯数字
        nm_id = own_nm.get(vc) or ""
        if not (nm_id and nm_id.isdigit()):
            print(f"  {tag} [跳过] {vc} 映射表无有效WB商品码，避免误上架")
            writer.writerow([now, vc, cn, nm_id, src_sid, ",".join(map(str, missing)), "-", price, "跳过", "映射表无有效WB商品码"])
            skip += 1
            continue

        if price is None:
            print(f"  {tag} [失败] {vc} 无价格数据")
            writer.writerow([now, vc, cn, nm_id, src_sid, ",".join(map(str, missing)), "-", price, "失败", "无价格数据"])
            fail += 1
            continue

        price_val = float(price)
        price_str = str(int(price_val)) if price_val.is_integer() else f"{price_val:.2f}"

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
            writer.writerow([now, vc, cn, nm_id, src_sid, ",".join(map(str, missing)), "-", price_str, "跳过", "已存在或无仓库"])
            skip += 1
            continue

        # 2) 包装数据提取：优先映射表 → 商品价格表兜底 → card.json 兜底（严禁使用快照数据）
        dims, err = resolve_replicate_package(vc, cn, nm_id, mapping_state, card_cache)
        if err:
            print(f"  {tag} [失败] {vc}: {err}")
            writer.writerow([now, vc, cn, nm_id, src_sid, ",".join(map(str, targets)), "-", price_str, "失败", err])
            fail += 1
            continue

        # 3) 前缀码决策
        prefix, prefix_src = resolve_vendor_prefix(cn, vc, cn2prefix)

        valid_items.append({
            "vc": vc,
            "cn": cn,
            "src_sid": src_sid,
            "targets": targets,
            "price_str": price_str,
            "sku": int(nm_id),
            "packageLength": dims[0],
            "packageWidth": dims[1],
            "packageHeight": dims[2],
            "weightBrut": dims[3],
            "vendorCodePrefix": prefix,
            "prefix_src": prefix_src,
            "stock": stock_for(cn, overrides),
        })

    # 2) 按目标店铺与库存分组，每组按 BATCH_SIZE (50) 分片推送到新批量上品接口
    groups = {}
    for it in valid_items:
        key = (tuple(sorted(it["targets"])), it["stock"])
        groups.setdefault(key, []).append(it)

    total_chunks = sum((len(items) + BATCH_SIZE - 1) // BATCH_SIZE for items in groups.values())
    curr_chunk = 0

    for (target_tuple, stock_qty), items in groups.items():
        shop_configs = [
            {"id": sid, "warehouseId": warehouses[sid], "warehouseQuantity": stock_qty}
            for sid in target_tuple
        ]
        target_str = ",".join(map(str, target_tuple))

        for chunk_idx in range(0, len(items), BATCH_SIZE):
            curr_chunk += 1
            chunk = items[chunk_idx:chunk_idx + BATCH_SIZE]
            sku_prices = [
                {
                    "sku": it["sku"],
                    "price": it["price_str"],
                    "packageLength": it["packageLength"],
                    "packageWidth": it["packageWidth"],
                    "packageHeight": it["packageHeight"],
                    "weightBrut": it["weightBrut"],
                    "vendorCodePrefix": it["vendorCodePrefix"],
                }
                for it in chunk
            ]

            print(f"\n[批次 {curr_chunk}/{total_chunks}] 推送 {len(chunk)} 个商品 → 店[{target_str}]（库存={stock_qty}）...")
            try:
                resp = bcs.batch_push_products(shop_configs, sku_prices, mode=1, carry_brand=1)
                now = datetime.now().strftime("%H:%M:%S")
                if resp.get("code") == 200:
                    task_id = (resp.get("data") or {}).get("taskId") or "已提交"
                    print(f"  ✓ 批次提交成功 (taskId={task_id})")
                    for it in chunk:
                        ok += 1
                        writer.writerow([now, it["vc"], it["cn"], it["sku"], it["src_sid"],
                                         target_str, it["vendorCodePrefix"], it["price_str"], "成功", f"taskId={task_id}"])
                        for sid in it["targets"]:
                            records.setdefault(it["vc"], {})[str(sid)] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _save_records(records)
                else:
                    msg = resp.get("msg") or f"code={resp.get('code')}"
                    print(f"  ✗ 批次提交失败: {msg}")
                    for it in chunk:
                        fail += 1
                        writer.writerow([now, it["vc"], it["cn"], it["sku"], it["src_sid"],
                                         target_str, it["vendorCodePrefix"], it["price_str"], "失败", msg])
            except Exception as e:
                now = datetime.now().strftime("%H:%M:%S")
                print(f"  ✗ 批次请求异常: {e}")
                for it in chunk:
                    fail += 1
                    writer.writerow([now, it["vc"], it["cn"], it["sku"], it["src_sid"],
                                     target_str, it["vendorCodePrefix"], it["price_str"], "失败", str(e)])

            if curr_chunk < total_chunks:
                time.sleep(getattr(args, "interval", 1.0))

    csv_file.close()
    print(f"\n[汇总] 计划 {len(plans)} | 成功 {ok} | 跳过 {skip} | 失败 {fail}（{time.time() - t0:.0f}s）")
    print(f"明细：{csv_path}")

    # ---- 写后验证：仅加 --sync 时 fetch 同步复核覆盖率（否则只打印提示，不自动做）----
    if ok > 0 and getattr(args, "sync", False) and not args.no_verify:
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
        from . import mapping_sync
        mapping_sync.post_write_merge(fetch=False)
    elif ok > 0:
        from . import mapping_sync
        mapping_sync.print_write_hint()
    return 0

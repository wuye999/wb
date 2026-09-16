# -*- coding: utf-8 -*-
"""
wb_ops 商品价格表 ↔ 店铺商品映射表构建（原 build_mapping.py 的数据逻辑）

数据职责：商品价格表解析、前缀映射、5 店并集分类、8-Sheet 映射表生成。
HTML 工作台渲染已抽到 workbench.py。
"""
import glob
import json
import math
import os
import re
from collections import defaultdict

import openpyxl

from wb_ops.adapters import bcs_client as bcs
from wb_ops import common
from wb_ops import config
from wb_ops.services.catalog import keywords
from wb_ops.services.catalog import workbench
# ---------------- 主店 / 常量（懒加载，避免 import 即联网） ----------------
_shop_id_cache = None


def shop_id():
    global _shop_id_cache
    if _shop_id_cache is None:
        _shop_id_cache = config.MAIN_SHOP or bcs.get_main_shop()
    return _shop_id_cache

from .mapping_excel import (
    IMG_COL,
    NMID_COL,
    detail_headers,
    row_for,
    build_xlsx,
)



from wb_ops.framework.safe_io import safe_load_json, atomic_dump_json
from wb_ops.storage.mapping_repo import (
    load_boss,
    load_prefix_map,
    load_vc_known,
    save_vc_known,
    load_vc_override,
    save_vc_override,
    load_vc_excluded,
    save_vc_excluded,
)



def apply_latest_dp(result_list):
    """用商品价格表最新双倍售价覆盖核对结果的 doublePrice（保证与商品价格表一致）"""
    boss = load_boss()
    dp_by_sku = {b["sku"]: b["dp"] for b in boss}
    n = 0
    for item in result_list:
        sku = item.get("sku")
        if sku in dp_by_sku and dp_by_sku[sku] is not None:
            if item.get("doublePrice") != dp_by_sku[sku]:
                item["doublePrice"] = dp_by_sku[sku]
                n += 1
    return result_list, n


def init_global_vc_pool():
    """冷启动初始化：若 vc_known.json 不存在，从现有「价格映射表.xlsx」提取 5600+ 映射关系，
    持久化到 data/state/vc_known.json 与 vc_excluded.json，确保历史资产零丢失。"""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if os.path.exists(config.VC_KNOWN_JSON):
        return
    state, excluded = load_mapping_state()
    known = {}
    for vc, st in state.items():
        known[vc] = {
            "cn": st.get("cn") or "",
            "dp": st.get("dp"),
            "L": st.get("L"),
            "W": st.get("W"),
            "H": st.get("H"),
            "weight": st.get("weight"),
            "sku": st.get("sku") or "",
            "source": "历史映射表迁移",
        }
    save_vc_known(known)
    print(f"[全局归属池] 冷启动初始化：已从现有映射表迁移 {len(known)} 个商品归属到 {config.VC_KNOWN_JSON}")

    if excluded and not os.path.exists(config.VC_EXCLUDED_JSON):
        save_vc_excluded(excluded)
        print(f"[全局排除清单] 已迁移 {len(excluded)} 个排除记录到 {config.VC_EXCLUDED_JSON}")


def load_vc_registry():
    """加载全局 VC 归属池与纠偏记录。返回 (known, overrides, excluded)。
    优先级：overrides（人工改名纠偏） > known（已知归属） > excluded（排除清单）。"""
    init_global_vc_pool()
    return load_vc_known(), load_vc_override(), load_vc_excluded()


def save_vc_registry(known=None, overrides=None, excluded=None):
    """保存全局 VC 归属、纠偏或排除记录"""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if known is not None:
        save_vc_known(known)
    if overrides is not None:
        save_vc_override(overrides)
    if excluded is not None:
        save_vc_excluded(excluded)


from .mapping_excel import (
    SINGLE_SHOP_HEADERS,
    SINGLE_SHOP_WIDTHS,
    SINGLE_UNMAPPED_HEADERS,
    SINGLE_UNMAPPED_WIDTHS,
    build_single_shop_xlsx,
    load_single_shop_rows,
    list_active_shop_mappings,
    stock_summary,
)



def load_mapping_state():
    """读现有映射表 xlsx → 增量状态（归属关系 + 已排除清单）。
    由 storage.mapping_repo.MappingRepository 托管。"""
    from wb_ops.storage.mapping_repo import MappingRepository
    return MappingRepository.load_mapping_state()



def load_bcs():
    """主店 JSON 在架商品，返回完整字段（附加 vc/wbnm/price/img 便捷字段）"""
    d = safe_load_json(config.shop_json_path(shop_id()), default={})
    rows = [r for r in d.get("rows", []) if not r.get("trashedAt")]
    out = []
    for r in rows:
        sl = r.get("sizeList") or []
        price = sl[0].get("price") if sl else None
        if price is None:
            continue
        c = dict(r)
        c["price"] = int(price)
        c["vc"] = r.get("vendorCode") or ""
        c["wbnm"] = str(r.get("nmId")) if r.get("nmId") else (common.extract_wb_nm(c["vc"]) or str(c["vc"]).rsplit("-", 1)[-1])
        c["img"] = r.get("repImg") or ""
        out.append(c)
    return out


def size_summary(r):
    """sizeList 压成 'chrtId(techSize)@price; ...'"""
    return "; ".join(f"{s.get('chrtId')}({s.get('techSizeName') or s.get('name') or ''})@{s.get('price')}"
                     for s in (r.get("sizeList") or []))



def price_of(r):
    """商品代表价 = sizeList[0].price"""
    sl = r.get("sizeList") or []
    return sl[0].get("price") if sl else None


# ---------------- 候选计算 ----------------
def load_shops_union():
    """5 店在架并集，按 vendorCode 去重合并（自包含读 JSON，避免循环 import）。
    返回 (union, shops_meta)：
      union: {vc: {'vc','title','price','img','per_shop':{sid:{'price','stock'}},'shops':[sid]}}
      过滤 trashedAt / 无价格（含空商品三缺）；同 vc 跨店合并，代表价取主店优先否则最小 sid 店。"""
    union = {}
    shops_meta = []
    try:
        meta = json.load(open(config.STATUS_JSON, encoding="utf-8"))
        shops_meta = meta.get("shops", []) or []
    except Exception:
        shops_meta = []
    if not shops_meta:  # 状态文件缺失 → 扫描目录推断店铺
        for p in sorted(glob.glob(config.shop_json_path("*"))):
            try:
                sid = int(os.path.basename(p).replace("shop", "").replace("_products_all.json", ""))
                shops_meta.append({"id": sid, "name": f"shop{sid}"})
            except ValueError:
                continue
    sid_main = shop_id()
    for s in shops_meta:
        sid = s["id"]
        p = config.shop_json_path(sid)
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8"))
        for r in d.get("rows", []):
            if r.get("trashedAt"):
                continue
            sl = r.get("sizeList") or []
            if not sl or sl[0].get("price") is None:
                continue  # 无价格（含空商品）不计
            vc = r.get("vendorCode")
            if not vc:
                continue
            price = sl[0].get("price")
            u = union.setdefault(vc, {"vc": vc, "title": r.get("title") or "", "img": r.get("repImg") or "",
                                      "per_shop": {}, "shops": [], "price": None})
            u["per_shop"][sid] = {"price": price, "stock": stock_summary(r),
                                  "nmId": r.get("nmId"), "createAt": r.get("createAt"),
                                  "updateAt": r.get("updateAt")}
            if sid not in u["shops"]:
                u["shops"].append(sid)
            if u["price"] is None or sid == sid_main:
                u["price"] = price
    return union, shops_meta


def classify_union(union, boss, prefix_map, known):
    """5 店并集 → 四分类（互斥，零重复）：
      cand_vcs:    价格命中候选池（代表价 ∈ 某商品 floor/floor+1）→ 上半区勾选
      auto_items:  前缀命中（中段 4 字母在商品价格表前缀码）→ merge 自动补录
      normal_items: 其余未归属 vc → 下半区卡片选归属
      skipped:     known（映射总表+已排除清单）跳过数
    返回 (cand_vcs, auto_items, normal_items, skipped)"""
    boss_floors = {b["floor"] for b in boss if b.get("floor") is not None}
    cand_vcs, auto_items, normal_items, skipped = [], [], [], 0
    for vc, u in sorted(union.items()):
        if vc in known:
            skipped += 1
            continue
        prices = [d["price"] for d in u["per_shop"].values() if d["price"] is not None]
        m = re.match(config.VC_PREFIX_RE, vc or "")
        boss_row = prefix_map.get(m.group(1)) if m else None
        if boss_row:
            auto_items.append({"vc": vc, "title": u["title"], "price": prices[0] if prices else None,
                               "shops": u["shops"], "prefix": m.group(1),
                               "bossSku": boss_row["sku"], "bossCn": boss_row["cn"], "bossDp": boss_row["dp"]})
            continue
        if u.get("price") is not None:
            p_int = int(u["price"])
            if p_int in boss_floors or (p_int - 1) in boss_floors:
                cand_vcs.append(vc)  # 价格命中 {floor, floor+1} → 上半区
                continue
        normal_items.append({"vc": vc, "title": u["title"], "price": prices[0] if prices else None,
                             "shops": u["shops"], "img": u["img"]})
    return cand_vcs, auto_items, normal_items, skipped


def known_vcs_from_mapping():
    """映射表已知 vc = 映射总表归属 + 已排除清单"""
    state, excluded = load_mapping_state()
    return set(state) | set(excluded)


def build_groups(boss, bcs):
    """按 floor 价分组；返回 (conflict_groups, single_groups, todo_list)"""
    by_floor = defaultdict(list)
    for b in boss:
        if b["floor"] is not None:
            by_floor[b["floor"]].append(b)

    price_index = defaultdict(list)
    for c in bcs:
        price_index[c["price"]].append(c)

    def cands_for(floor_p):
        """候选 = 价格 ∈ {floor_p, floor_p+1} 的商品，附关键词命中数"""
        pool = price_index.get(floor_p, []) + price_index.get(floor_p + 1, [])
        seen, out = set(), []
        for c in pool:
            if c["vc"] in seen:
                continue
            seen.add(c["vc"])
            c = dict(c)
            c["hit"] = 0
            out.append(c)
        return out

    def with_hits(b, cands):
        kws = keywords.keywords_for(b["cn"])
        pats = [re.compile(k, re.I) for k in kws]
        for c in cands:
            c["hit"] = sum(1 for p in pats if p.search(c["title"]))
        return cands

    conflict_groups, single_groups, todo = [], [], []
    for floor_p, bosses in sorted(by_floor.items()):
        cands = cands_for(floor_p)
        if not cands:  # 价格带内无任何店铺商品 → 待核查
            for b in bosses:
                todo.append(b)
            continue
        for b in bosses:
            with_hits(b, cands)
        if len(bosses) > 1:
            conflict_groups.append({"floor": floor_p, "bosses": bosses, "cands": cands})
        else:
            single_groups.append({"floor": floor_p, "bosses": bosses, "cands": cands})
    return conflict_groups, single_groups, todo


# ---------------- 自动预匹配 ----------------
def auto_match(result_list, bcs):
    """自动预匹配（v2，冲突检测）。返回 {idx: [vc, ...]}"""
    price_index = defaultdict(list)
    for c in bcs:
        price_index[c["price"]].append(c)

    hits = {}
    for item in result_list:
        dp = item.get("doublePrice")
        if dp is None:
            continue
        floor = int(dp)
        pool = price_index.get(floor, []) + price_index.get(floor + 1, [])
        kws = keywords.keywords_for(item.get("cn") or "")
        pats = [re.compile(k, re.I) for k in kws]
        s = {c["vc"] for c in pool if any(p.search(c["title"]) for p in pats)}
        if s:
            hits[item["idx"]] = s

    vc_owner = defaultdict(list)
    for idx, s in hits.items():
        for vc in s:
            vc_owner[vc].append(idx)
    conflict_vcs = {vc for vc, owners in vc_owner.items() if len(owners) > 1}

    out = {}
    for idx, s in hits.items():
        clean = [vc for vc in s if vc not in conflict_vcs]
        if clean:
            out[idx] = clean
    return out





# ---------------- 入口逻辑（供 cli 调用） ----------------
def run_mapping(legacy=False):
    """生成核对工作台。legacy=True 仅主店候选；否则 5 店并集一页两区。"""
    boss = load_boss()

    if legacy:
        bcs_data = load_bcs()
        conflict_groups, single_groups, todo = build_groups(boss, bcs_data)
        n_conf, n_single, n_todo = workbench.render_html(conflict_groups, single_groups, todo)
        print(f"商品价格表商品 {len(boss)} 个 → 冲突组 {n_conf} 个({sum(len(g['bosses']) for g in conflict_groups)} 商品)"
              f" · 普通 {n_single} 个 · 待核查 {n_todo} 个: {[b['cn'] for b in todo]}")
        print(f"HTML 工作台已生成：{config.OUT_MAPPING_HTML}（legacy：仅主店）")
        return

    union, shops_meta = load_shops_union()
    if not union:
        raise RuntimeError("未找到任何店铺数据，请先运行 wb.py fetch")
    known = known_vcs_from_mapping()
    prefix_map = load_prefix_map()
    cand_vcs, auto_items, normal_items, skipped = classify_union(union, boss, prefix_map, known)
    cand_items = [union[vc] for vc in cand_vcs]
    conflict_groups, single_groups, todo = build_groups(boss, cand_items)
    stats = {"cand": len(cand_vcs), "normal": len(normal_items),
             "auto": len(auto_items), "known": skipped,
             "shops": [s["id"] for s in shops_meta]}
    n_conf, n_single, n_todo, n_normal = workbench.render_unified_html(
        conflict_groups, single_groups, todo, normal_items, shops_meta, stats)

    n_union = len(union)
    n_sum = len(cand_vcs) + len(normal_items) + len(auto_items)
    ident = "✓" if n_union - skipped == n_sum else "✗"
    print(f"5 店并集 {n_union} · 已知(映射表)跳过 {skipped} · 候选池 {len(cand_vcs)} · 未归属新 vc {len(normal_items)}"
          f" · 前缀自动 {len(auto_items)}")
    print(f"  恒等式 |union|-|known|==cand+normal+auto：{n_union}-{skipped}={n_sum} {ident}")
    print(f"  商品价格表商品 {len(boss)} → 冲突组 {n_conf} · 普通 {n_single} · 待核查 {n_todo} · 下半区卡片 {n_normal}")
    print(f"HTML 统一工作台已生成：{config.OUT_MAPPING_HTML}（导出 vc 中心格式 → wb.py merge 统一审核.json）")


def import_mapping(import_file, auto=False):
    """导入核对结果 JSON → 生成映射表 xlsx（旧格式，单店初建）。"""
    result = json.load(open(import_file, encoding="utf-8"))
    if isinstance(result, dict) and "rows" in result:
        result = result["rows"]
    bcs_data = load_bcs()
    result, n_dp = apply_latest_dp(result)
    if n_dp:
        print(f"[双倍售价同步] 覆盖 {n_dp} 个商品为商品价格表最新值")
    if auto:
        auto_map = auto_match(result, bcs_data)
        n_auto = 0
        for item in result:
            if not item.get("vendorCodes") and item["idx"] in auto_map:
                item["vendorCodes"] = auto_map[item["idx"]]
                item["auto"] = True
                n_auto += 1
        print(f"[自动预匹配] 补充 {n_auto} 个商品（仅参考，非正式映射）")
    n_boss, n_pick, n_unmap, n_multi = build_xlsx(result, [], bcs_data)
    print(f"映射表已生成：{config.MAPPING_XLSX}")
    print(f"  商品价格表商品 {n_boss} 个 · 勾选 vendorCode {n_pick} 个"
          f" · 未映射在架 {n_unmap} 个 · 多重映射冲突 {n_multi} 个")

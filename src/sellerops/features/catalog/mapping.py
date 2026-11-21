"""构建商品归属工作台，导入旧格式核对结果。"""
from collections import defaultdict
from functools import lru_cache
import math
import os
import re

from sellerops.core import utils as common
from sellerops.core.files import safe_load_json
from sellerops.domain.snapshot_values import first_price, valid_sizes
from sellerops.features.catalog import keywords, workbench
from sellerops.features.catalog.mapping_excel import build_xlsx, stock_summary
from sellerops.integrations.bcs.client import client as bcs
from sellerops.persistence.mapping_batch import mapping_batch
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.settings import paths as config


@lru_cache(maxsize=1)
def main_shop(data: config.DataPaths):
    return config.MAIN_SHOP or bcs.get_main_shop()


def apply_latest_dp(result_list):
    """用商品价格表最新实际售价覆盖核对结果的 doublePrice（dict 键沿用 doublePrice）"""
    boss = MappingRepository.load_price_book()
    dp_by_sku = {b["sku"]: b["dp"] for b in boss}
    n = 0
    for item in result_list:
        sku = item.get("sku")
        if sku in dp_by_sku and dp_by_sku[sku] is not None:
            if item.get("doublePrice") != dp_by_sku[sku]:
                item["doublePrice"] = dp_by_sku[sku]
                n += 1
    return result_list, n


def load_vc_registry():
    """在内存中补全旧映射表的历史归属。由映射写命令统一保存。"""
    known = MappingRepository.load_vc_known()
    excluded = MappingRepository.load_vc_excluded()
    overrides = MappingRepository.load_vc_override()
    if not all(isinstance(values, dict) for values in (known, overrides, excluded)):
        raise ValueError("商品归属、纠偏或排除文件格式错误")
    if not os.path.exists(config.current.vc_known_json):
        state, historical_exclusions = MappingRepository.load_mapping_state()
        for vc, row in state.items():
            known[vc] = {key: row.get(key) for key in ("cn", "dp", "L", "W", "H", "weight", "sku")}
            known[vc].update(cn=row.get("cn") or "", sku=row.get("sku") or "", source="历史映射表迁移")
        if not os.path.exists(config.current.vc_excluded_json):
            excluded.update(historical_exclusions)
    return known, overrides, excluded


def load_bcs():
    """主店 JSON 在架商品，返回完整字段（附加 vc/wbnm/price/img 便捷字段）"""
    document = ProductSnapshotRepository.load_snapshot_document(main_shop(config.current))
    rows = document["rows"] if document is not None else []
    return priced_rows(rows)


def priced_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("trashedAt"):
            continue
        price = first_price(r)
        if price is None:
            continue
        c = dict(r)
        c["price"] = price
        c["vc"] = r.get("vendorCode") or ""
        c["wbnm"] = str(r.get("nmId")) if r.get("nmId") else (common.extract_wb_nm(c["vc"]) or str(c["vc"]).rsplit("-", 1)[-1])
        c["img"] = r.get("repImg") or ""
        out.append(c)
    return out


def size_summary(r):
    """sizeList 压成 'chrtId(techSize)@price; ...'"""
    return "; ".join(f"{s.get('chrtId')}({s.get('techSizeName') or s.get('name') or ''})@{s.get('price')}"
                     for s in (valid_sizes(r) or []))


def price_of(r):
    """商品代表价 = sizeList[0].price"""
    return first_price(r)


# ---------------- 候选计算 ----------------
def load_shops_union():
    """全部活跃店铺在架并集，按 vendorCode 去重合并（自包含读 JSON，避免循环 import）。
    返回 (union, shops_meta)：
      union: {vc: {'vc','title','price','img','per_shop':{sid:{'price','stock'}},'shops':[sid]}}
      过滤 trashedAt / 无价格（含空商品三缺）；同 vc 跨店合并，代表价取主店优先否则最小 sid 店。"""
    union = {}
    meta = safe_load_json(config.current.status_json, default={})
    shops_meta = meta.get("shops", []) or []
    if not shops_meta:  # 状态文件缺失 → 扫描目录推断店铺
        for sid in ProductSnapshotRepository.shop_ids_from_disk():
            shops_meta.append({"id": sid, "name": f"shop{sid}"})
    sid_main = main_shop(config.current)
    for s in shops_meta:
        sid = s["id"]
        p = config.shop_json_path(sid)
        if not os.path.exists(p):
            continue
        d = safe_load_json(p, default={})
        for r in d.get("rows", []):
            if r.get("trashedAt"):
                continue
            price = first_price(r)
            if price is None:
                continue  # 无价格（含空商品）不计
            vc = r.get("vendorCode")
            if not vc:
                continue
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
    """全部活跃店铺并集 → 四分类（互斥，零重复）：
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
    state, excluded = MappingRepository.load_mapping_state()
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
        try:
            floor = math.floor(float(dp))
        except (TypeError, ValueError):
            continue
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
def run_mapping(main_shop_only=False):
    """生成核对工作台。main_shop_only=True 仅主店候选；否则全部活跃店铺并集一页两区。"""
    boss = MappingRepository.load_price_book()

    if main_shop_only:
        bcs_data = load_bcs()
        conflict_groups, single_groups, todo = build_groups(boss, bcs_data)
        n_conf, n_single, n_todo = workbench.render_html(conflict_groups, single_groups, todo)
        print(f"商品价格表商品 {len(boss)} 个 → 冲突组 {n_conf} 个({sum(len(g['bosses']) for g in conflict_groups)} 商品)"
              f" · 普通 {n_single} 个 · 待核查 {n_todo} 个: {[b['cn'] for b in todo]}")
        print(f"HTML 工作台已生成：{config.current.out_mapping_html}（main_shop_only：仅主店）")
        return

    union, shops_meta = load_shops_union()
    if not union:
        raise RuntimeError("未找到任何店铺数据，请先运行 sellerops catalog fetch")
    known = known_vcs_from_mapping()
    prefix_map = MappingRepository.load_prefix_map()
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
    print(f"{len(shops_meta)} 店并集 {n_union} · 已知(映射表)跳过 {skipped} · 候选池 {len(cand_vcs)} · 未归属新 vc {len(normal_items)}"
          f" · 前缀自动 {len(auto_items)}")
    print(f"  恒等式 |union|-|known|==cand+normal+auto：{n_union}-{skipped}={n_sum} {ident}")
    print(f"  商品价格表商品 {len(boss)} → 冲突组 {n_conf} · 普通 {n_single} · 待核查 {n_todo} · 下半区卡片 {n_normal}")
    print(f"HTML 统一工作台已生成：{config.current.out_mapping_html}（导出 vc 中心格式 → sellerops catalog merge 统一审核.json）")


def import_mapping(import_file, auto=False):
    """导入核对结果 JSON → 生成映射表 xlsx（旧格式，单店初建）。"""
    with mapping_batch("import-mapping", input_file=import_file, auto=auto) as batch:
        if batch is None:
            return
        result = batch.input_data
        if isinstance(result, dict) and "rows" in result:
            result = result["rows"]
        if not isinstance(result, list):
            raise ValueError("核对文件应包含旧版工作台导出的商品列表")
        for row in result:
            if not isinstance(row, dict) or "action" in row and "vendorCodes" not in row:
                raise ValueError("旧格式核对结果应由旧版工作台导出；多店审核结果请使用 catalog merge")
            if row.get("vendorCodes") is not None and not isinstance(row["vendorCodes"], list):
                raise ValueError("旧格式核对记录的 vendorCodes 应为列表")
        if not os.path.isfile(config.current.price_book_xlsx):
            raise FileNotFoundError(config.current.price_book_xlsx)
        bcs_data = load_bcs()
        result, n_dp = apply_latest_dp(result)
        if n_dp:
            print(f"[实际售价同步] 覆盖 {n_dp} 个商品为商品价格表最新值")
        if auto:
            auto_map = auto_match(result, bcs_data)
            n_auto = 0
            for item in result:
                if not item.get("vendorCodes") and item["idx"] in auto_map:
                    item["vendorCodes"] = auto_map[item["idx"]]
                    item["auto"] = True
                    n_auto += 1
            print(f"[自动预匹配] 补充 {n_auto} 个商品（仅参考，非正式映射）")
        n_boss, n_pick, n_unmap, n_multi = build_xlsx(result, [], bcs_data, out_path=batch.stage(config.current.mapping_xlsx))
    print(f"映射表已生成：{config.current.mapping_xlsx}")
    print(f"  商品价格表商品 {n_boss} 个 · 勾选 vendorCode {n_pick} 个"
          f" · 未映射在架 {n_unmap} 个 · 多重映射冲突 {n_multi} 个")

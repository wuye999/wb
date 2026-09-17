# -*- coding: utf-8 -*-
"""
wb_ops 查询 WB 平台投诉单（appeals，只读）

口径（2026-09-16 与用户确认）：
- 未处理 = status_id == 1（等待回复）
- --days N = 剩余天数「恰好等于 N」（平台字段 decide_counter，即后台界面显示的剩余天数）
- 输出 = 控制台明细表 + 商品编号（nmId）与**供应商代码（vendorCode）**两行英文逗号分隔去重清单 + CSV
- 商品编号（nmId）仅由投诉详情 brands[].products[].nmid 下发，故必须逐条调详情；
  平台可能因此把投诉标记为「已读」（is_read），属浏览副作用，已确认接受。

供应商代码 / 中文名解析：**只用本地真源**（本店在架快照 nmId→vc，兜底映射总表 nmId 字段），
两处都查不到 → 如实标注「本地真源未收录」，不做联网核实（用户口径）。

鉴权：WB cookie 三件套会话（wb_api.make_session）；只读，不写 BCS、不改任何平台业务状态。
"""
import csv
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import callcenter_client as cc_api
from wb_ops.adapters import wb_client as wb_api
from wb_ops.domain.models import Complaint, ComplaintProduct
from wb_ops.storage.mapping_repo import MappingRepository
from wb_ops.storage.product_repo import ProductSnapshotRepository

PENDING_STATUS_ID = 1      # 「等待回复」= 未处理
MISS_LABEL = "本地真源未收录"  # nmId 在本店快照与映射表中均查不到（不联网核实）
SHOP_SLEEP = 0.5           # 店间间隔（串行）
DETAIL_SLEEP = 0.3         # 详情接口逐条间隔
CSV_FIELDS = [
    "店铺", "店铺ID", "投诉ID", "投诉方", "主题", "父主题", "创建时间",
    "状态ID", "状态", "剩余天数", "是否已读", "是否关闭",
    "商品nmId", "供应商代码", "解析来源", "商品中文名", "商品标题", "商品图片",
]


class _LocalResolver:
    """按店本地反查 nmId → 供应商代码 / 中文名（一次读快照 + 映射池，失败静默降级）

    真源优先级：
      ① 本店在架快照 `{vendorCode: row}` 的 nmId 字段（最准，含该店真实在架商品）
      ② 映射总表 state 的 nmId 字段（跨店兜底：vc 为跨店唯一键，商品可能在别的店在架）
    两处都没有 → MISS_LABEL（不联网核实）。
    """

    def __init__(self, shop_id: int, with_cn: bool = True):
        self.nm2vc: Dict[int, str] = {}
        self.nm2src: Dict[int, str] = {}
        self._cn = (lambda vc: "")
        try:
            resolve_cn, _ = MappingRepository.build_vc_resolver()
            for vc, item in (ProductSnapshotRepository.load_shop_rows(shop_id) or {}).items():
                nm_id = common.to_int(item.get("nmId") or item.get("nmID"))
                if nm_id:
                    self.nm2vc[nm_id] = vc
                    self.nm2src[nm_id] = "店快照"
            state, _ = MappingRepository.load_mapping_state()
            for vc, info in state.items():
                nm_id = common.to_int((info or {}).get("nmId"))
                if nm_id and nm_id not in self.nm2vc:
                    self.nm2vc[nm_id] = vc
                    self.nm2src[nm_id] = "映射表"
            if with_cn:
                self._cn = lambda vc: resolve_cn(vc, "")
        except Exception:
            pass

    def resolve(self, nm_id: int) -> Dict[str, str]:
        """nm_id → {'vc': 供应商代码, 'cn': 中文名, 'src': 解析来源}"""
        key = common.to_int(nm_id)
        vc = self.nm2vc.get(key, "")
        return {"vc": vc, "cn": self._cn(vc) if vc else "", "src": self.nm2src.get(key, "")}


def _extract_products(detail: Optional[Dict[str, Any]]) -> List[ComplaintProduct]:
    """从投诉详情提取关联商品（遍历全部 brands[].products[]）"""
    if not detail:
        return []
    out: List[ComplaintProduct] = []
    for brand in (detail.get("brands") or []):
        if not isinstance(brand, dict):
            continue
        brand_name = str(brand.get("name") or "")
        for p in (brand.get("products") or []):
            if not isinstance(p, dict):
                continue
            nm_id = common.to_int(p.get("nmid"))
            if not nm_id:
                continue
            out.append(ComplaintProduct(
                nm_id=nm_id,
                name=str(p.get("name") or ""),
                brand=brand_name,
                image_url=str(p.get("image_url") or ""),
            ))
    return out


def _pick_pending(listed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按未处理口径筛选：status_id == 1（等待回复）"""
    return [it for it in listed if common.to_int(it.get("status_id")) == PENDING_STATUS_ID]


def _filter_by_days(pending: List[Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    """按剩余天数精确筛选（days=0 表示不筛选）。decide_counter 缺失的条目不命中"""
    if not days:
        return pending
    return [it for it in pending
            if it.get("decide_counter") is not None
            and common.to_int(it.get("decide_counter")) == days]


def run(args: Any) -> int:
    """查询各店未处理投诉，输出明细 + 去重商品编号/供应商代码 + CSV（只读）"""
    common.ensure_utf8_stdout()
    cred = credentials.get()
    wb_shops = cred.wb_shops()
    if not wb_shops:
        print("[错误] credentials.json 没有已填 cookie 的店铺")
        return 1
    pairs = [(s, common.to_int(s.get("shopId"))) for s in wb_shops]
    if getattr(args, "shops", ""):
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        pairs = [p for p in pairs if p[1] in want]
    if not pairs:
        print("[错误] 没有匹配的店铺（检查 --shops 或 credentials.json）")
        return 1

    appeal_type = getattr(args, "type", "in") or "in"
    days = common.to_int(getattr(args, "days", 0))
    limit = common.to_int(getattr(args, "limit", 0))
    with_cn = not bool(getattr(args, "no_cn", False))

    names = ", ".join(f"{s['shopName']}({sid})" for s, sid in pairs)
    day_kw = f"｜剩余天数={days}" if days else "｜全部剩余天数"
    limit_kw = f"｜每店限拉 {limit} 条" if limit else ""
    print(f"店铺 {len(pairs)} 个: {names}"
          f"（只读；未处理=等待回复({PENDING_STATUS_ID})｜type={appeal_type}{day_kw}{limit_kw}）")
    if limit:
        print(f"  [提示] --limit {limit} 仅限制每店列表拉取条数，结果可能不完整；要全量请去掉 --limit")

    rows: List[Dict[str, Any]] = []
    all_nmids: Set[int] = set()
    all_vcs: Set[str] = set()
    missing: Set[int] = set()
    matched_total = 0
    for shop, sid in pairs:
        name = shop["shopName"]
        resolver = _LocalResolver(sid, with_cn=with_cn)
        stats: Dict[str, Any] = {}
        try:
            session = wb_api.make_session(shop, cred.root_version)
            listed = cc_api.fetch_appeals(session, appeal_type=appeal_type, limit=limit, stats=stats)
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {name}: {e}（该店中止，继续下一店；cookie 失效请跑 wb.py cookies-update）")
            continue
        except Exception as e:
            print(f"  [警告] 店铺 {name} 拉取投诉单失败: {e}（该店中止，继续下一店）")
            continue

        pending = _pick_pending(listed)
        matched = _filter_by_days(pending, days)
        matched_total += len(matched)
        scope = f"列表 {len(listed)} 条" + (f"/共 {stats.get('total')} 条" if stats.get("total") else "")
        if not pending:
            print(f"\n=== {name}({sid}) 无未处理投诉（{scope}）===")
            time.sleep(SHOP_SLEEP)
            continue
        if not matched:
            print(f"\n=== {name}({sid}) 无剩余天数={days} 的未处理投诉"
                  f"（等待回复 {len(pending)} 条｜{scope}）===")
            time.sleep(SHOP_SLEEP)
            continue
        print(f"\n=== {name}({sid}) 未处理投诉 {len(matched)} 条"
              f"（等待回复共 {len(pending)} 条｜{scope}）===")

        for idx, item in enumerate(matched, 1):
            appeal_id = common.to_int(item.get("id"))
            try:
                detail = cc_api.fetch_appeal_detail(session, appeal_id)
            except common.CookieExpiredError as e:
                print(f"    [警告] 投诉 {appeal_id} 详情失败: {e}（该店剩余条目中止）")
                break
            if detail is None:
                print(f"    [警告] 投诉 {appeal_id} 详情获取失败（nmId 缺失，该条跳过提取）")
            products = _extract_products(detail)
            complaint = Complaint.from_list_dict(sid, name, item, products=tuple(products))
            nmids, vcs, cns, srcs = [], [], [], set()
            for p in products:
                info = resolver.resolve(p.nm_id)
                nmids.append(p.nm_id)
                all_nmids.add(p.nm_id)
                if info["vc"]:
                    vcs.append(info["vc"])
                    all_vcs.add(info["vc"])
                    srcs.add(info["src"])
                else:
                    missing.add(p.nm_id)
                if info["cn"]:
                    cns.append(info["cn"])
            nmids_s = ",".join(str(x) for x in nmids)
            vcs_s = ",".join(vcs) or MISS_LABEL
            cns_s = ",".join(cns)
            counter_s = "" if complaint.decide_counter is None else f"{complaint.decide_counter}天"
            rows.append({
                "店铺": name, "店铺ID": sid, "投诉ID": appeal_id,
                "投诉方": complaint.cro_company, "主题": complaint.theme_name,
                "父主题": complaint.parent_theme_name, "创建时间": complaint.create_date,
                "状态ID": complaint.status_id, "状态": complaint.status,
                "剩余天数": "" if complaint.decide_counter is None else complaint.decide_counter,
                "是否已读": "是" if complaint.is_read else "否",
                "是否关闭": "是" if complaint.is_closed else "否",
                "商品nmId": nmids_s, "供应商代码": vcs_s,
                "解析来源": "/".join(sorted(srcs)) or MISS_LABEL,
                "商品中文名": cns_s,
                "商品标题": " | ".join(p.name for p in products if p.name),
                "商品图片": " ".join(p.image_url for p in products if p.image_url),
            })
            print(f"  [列表] #{idx} id={appeal_id} | 投诉方:{complaint.cro_company or '-'}"
                  f" | 主题:{complaint.theme_name or '-'}"
                  f" | 创建:{complaint.create_date or '-'}"
                  f" | 状态:{complaint.status}({complaint.status_id})"
                  f" | 剩余:{counter_s or '-'}"
                  f" | nmId:{nmids_s or '（详情缺失）'}"
                  f" | 供应商代码:{vcs_s}"
                  f"{' | 中文名:' + cns_s if cns_s else ''}")
            time.sleep(DETAIL_SLEEP)
        time.sleep(SHOP_SLEEP)

    print(f"\n[汇总] 未处理投诉（等待回复）命中 {matched_total} 条；明细 {len(rows)} 行；"
          f"去重商品 nmId {len(all_nmids)} 个；去重供应商代码 {len(all_vcs)} 个")
    if missing:
        print(f"[提示] {len(missing)} 个商品在本店快照与映射表中均未收录（本地真源无此商品，未做联网核实）：")
        print("  " + ",".join(str(x) for x in sorted(missing)))
    if all_nmids:
        print("[编号] 商品编号 nmId（英文逗号分隔，可直接复制）：")
        print(",".join(str(x) for x in sorted(all_nmids)))
    if all_vcs:
        print("[供应商代码] vendorCode（英文逗号分隔，可直接复制）：")
        print(",".join(sorted(all_vcs)))

    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"投诉单_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] 命中 {len(rows)} 行 → {path}")
    else:
        print("\n没有匹配的未处理投诉")
    return 0

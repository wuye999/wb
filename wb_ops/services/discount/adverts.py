# -*- coding: utf-8 -*-
"""
wb_ops 查询 WB 广告推广（cmp.wildberries.ru）中被推广的商品（只读）

口径（2026-09-23 与用户确认）：
- 「推广」= cmp.wildberries.ru 广告推广活动；被推广商品内嵌在 `adverts[].stocks.products[]`
  （`show_stocks=true` 才下发），字段 nm（WB 商品码）/ name（俄文标题）/ subject.name（类目）/
  total_quantity_fbo|mp（活动内库存）。
- 活动状态默认 `status=[4,9,11]`，与后台默认视图一致。
- 输出 = 控制台「活动 → 商品」明细 + 去重 WB商品码(nmId) / 供应商代码(vendorCode) 可复制清单 + CSV。

供应商代码 / 中文名解析：**只用本地真源**（`storage.NmResolver`：本店在架快照 nmId→vc，
兜底映射总表 nmId），两处都查不到 → 如实标注「本地真源未收录」，不做联网核实（与 appeals 同口径）。

鉴权：WB cookie 三件套会话（`wb_api.make_session`）+ cmp 域必需头（`wb_ads_client.cmp_headers`）。
抓包来源：api/网络请求/wb推广活动列表.har（2026-09-23）。全程只读，不写 BCS、不改任何平台业务状态。
"""
import csv
import os
import time
from typing import Any, Dict, List, Set, Tuple

from wb_ops import common
from wb_ops import config
from wb_ops import credentials
from wb_ops.adapters import wb_ads_client as ads_api
from wb_ops.adapters import wb_client as wb_api
from wb_ops.storage.nm_resolver import MISS_LABEL, NmResolver

SHOP_SLEEP = 0.5           # 店间间隔（串行）
CSV_FIELDS = [
    "店铺", "店铺ID", "活动ID", "活动名", "活动状态ID", "结算方式", "预算", "创建时间",
    "商品nmId", "供应商代码", "解析来源", "商品中文名", "俄文标题", "商品类目",
    "活动内FBO库存", "活动内MP库存",
]


def iter_campaign_products(campaigns: Any) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """展平 `content[].stocks.products[]` → [(campaign, product), ...]（无商品的活动跳过）。"""
    out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for c in (campaigns or []):
        if not isinstance(c, dict):
            continue
        stocks = c.get("stocks")
        products = (stocks or {}).get("products") if isinstance(stocks, dict) else None
        for p in (products or []):
            if isinstance(p, dict):
                out.append((c, p))
    return out


def missing_detail_campaigns(campaigns: Any) -> List[int]:
    """`products_count > 0` 但 `stocks.products` 为空的活动 ID（明细缺失，调用方需打 [警告]）。"""
    out: List[int] = []
    for c in (campaigns or []):
        if not isinstance(c, dict):
            continue
        if common.to_int(c.get("products_count")) <= 0:
            continue
        stocks = c.get("stocks")
        products = (stocks or {}).get("products") if isinstance(stocks, dict) else None
        if not products:
            out.append(common.to_int(c.get("id")))
    return out


def build_options(args: Any) -> Dict[str, Any]:
    """CLI args → 运行选项（含 --status 解析与默认值兜底，便于库调用与离线单测）。"""
    raw = (getattr(args, "status", "") or "").strip()
    statuses: Tuple[int, ...] = ads_api.STATUS_DEFAULT
    if raw:
        parsed = tuple(common.to_int(x, -1) for x in raw.split(",") if x.strip())
        parsed = tuple(s for s in parsed if s >= 0)
        if parsed:
            statuses = parsed
    return {
        "statuses": statuses,
        "with_cn": not bool(getattr(args, "no_cn", False)),
        "page_size": common.to_int(getattr(args, "page_size", 100), 100) or 100,
        "limit": common.to_int(getattr(args, "limit", 0)),
        "max_pages": common.to_int(getattr(args, "max_pages", 50), 50) or 50,
    }


def process_shop(shop: Dict[str, Any], root_version: str, opt: Dict[str, Any],
                 agg: Dict[str, Any]) -> None:
    """单店：拉推广活动 → 展平商品 → 本地反查 → 打印明细 + 累积统计。"""
    sid = common.to_int(shop.get("shopId") or shop.get("shop_id"))
    name = shop.get("shopName") or f"shop_{sid}"
    rows: List[Dict[str, Any]] = agg["rows"]

    resolver = NmResolver(sid, with_cn=opt["with_cn"])
    session = wb_api.make_session(shop, root_version)
    if not os.path.exists(config.shop_json_path(sid)):
        print(f"  [警告] 本店无 BCS 快照（{config.shop_json_path(sid)} 不存在）→ "
              f"供应商代码/中文名仅靠映射总表兜底；需最新请先跑 python wb.py fetch")

    campaigns, total = ads_api.fetch_adverts(
        session, shop, statuses=opt["statuses"], page_size=opt["page_size"],
        limit=opt["limit"], max_pages=opt["max_pages"])
    agg["campaigns"] += len(campaigns)
    print(f"\n=== 店铺 {name}({sid}) ===  活动 {len(campaigns)} 个"
          + (f"（平台口径 {total} 个）" if total >= 0 else ""))
    if total >= 0 and not opt["limit"] and len(campaigns) < total:
        print(f"  [警告] 分页未取全：平台口径 {total} 个，实取 {len(campaigns)} 个（可加 --page-size 重试）")
    for cid in missing_detail_campaigns(campaigns):
        print(f"  [警告] 活动 {cid} 声明有商品但未下发明细（stocks.products 为空），该活动不产出商品行")

    flat = iter_campaign_products(campaigns)
    if not flat:
        print("  （该店没有可列出的被推广商品）")
        return

    camp_by_id = {c.get("id"): c for c in campaigns if isinstance(c, dict)}
    order: List[Any] = []
    by_camp: Dict[Any, List[Dict[str, Any]]] = {}
    for c, p in flat:
        cid = c.get("id")
        if cid not in by_camp:
            by_camp[cid] = []
            order.append(cid)
        by_camp[cid].append(p)

    for cid in order:
        c = camp_by_id.get(cid) or {}
        products = by_camp[cid]
        print(f"  活动 [{cid}] 「{c.get('campaign_name') or '-'}」 "
              f"status={common.to_int(c.get('status_id'))} {c.get('payment_model') or ''} "
              f"预算={c.get('budget')} 商品 {len(products)} 个")
        for p in products:
            nm_id = common.to_int(p.get("nm"))
            info = resolver.resolve(nm_id)
            vc = info["vc"] or MISS_LABEL
            cn = info["cn"]
            if nm_id:
                agg["nmids"].add(nm_id)
                if not info["vc"]:
                    agg["missing"].add(nm_id)
            if info["vc"]:
                agg["vcs"].add(info["vc"])
            subject = ((p.get("subject") or {}).get("name") or "") if isinstance(p.get("subject"), dict) else ""
            rows.append({
                "店铺": name, "店铺ID": sid,
                "活动ID": cid,
                "活动名": c.get("campaign_name") or "",
                "活动状态ID": common.to_int(c.get("status_id")),
                "结算方式": c.get("payment_model") or "",
                "预算": c.get("budget") if c.get("budget") is not None else "",
                "创建时间": (c.get("create_date") or "")[:19].replace("T", " "),
                "商品nmId": nm_id or "",
                "供应商代码": vc,
                "解析来源": info["src"] or MISS_LABEL,
                "商品中文名": cn,
                "俄文标题": p.get("name") or "",
                "商品类目": subject,
                "活动内FBO库存": common.to_int(p.get("total_quantity_fbo")),
                "活动内MP库存": common.to_int(p.get("total_quantity_mp")),
            })
            print(f"      nm={nm_id} | 供应商代码={vc} | 中文名={cn or '-'}"
                  f" | {p.get('name') or '-'}" + (f" [{subject}]" if subject else ""))


def run(args: Any) -> int:
    """查询各店广告推广中被推广的商品，输出明细 + 去重清单 + CSV（只读）。"""
    common.ensure_utf8_stdout()
    cred = credentials.get()
    shops = cred.wb_shops()
    if not shops:
        print("[错误] credentials.json 中没有已填 cookie 的店铺（请编辑 data/credentials.json 的 wb.shops）")
        return 1
    if getattr(args, "shops", ""):
        want = {int(x) for x in args.shops.split(",") if x.strip()}
        shops = [s for s in shops if common.to_int(s.get("shopId")) in want]
        if not shops:
            print(f"[错误] 指定店铺 {args.shops} 未在凭证中找到已填 cookie 的店铺")
            return 1

    opt = build_options(args)
    root_version = cred.root_version
    names = ", ".join(f"{s.get('shopName')}({s.get('shopId')})" for s in shops)
    print(f"店铺 {len(shops)} 个: {names}"
          f"（只读；广告推广 cmp.wildberries.ru | 活动状态 {list(opt['statuses'])}"
          f" | 商品中文名{'不解析' if not opt['with_cn'] else '解析'}）")

    agg: Dict[str, Any] = {"rows": [], "nmids": set(), "vcs": set(), "missing": set(), "campaigns": 0}
    for shop in shops:
        try:
            process_shop(shop, root_version, opt, agg)
        except common.CookieExpiredError as e:
            print(f"  [警告] 店铺 {shop.get('shopName')}: {e}"
                  f"（该店中止，继续下一店；cookie 失效请跑 wb.py cookies-update）")
        except Exception as e:
            print(f"  [警告] 店铺 {shop.get('shopName')} 处理失败: {e}（该店中止，继续下一店）")
        time.sleep(SHOP_SLEEP)

    rows = agg["rows"]
    nmids: Set[int] = agg["nmids"]
    vcs: Set[str] = agg["vcs"]
    print(f"\n[汇总] 推广活动 {agg['campaigns']} 个 | 商品行 {len(rows)} 行 | "
          f"去重 WB商品码 {len(nmids)} 个 | 去重供应商代码 {len(vcs)} 个")
    if agg["missing"]:
        print(f"[提示] {len(agg['missing'])} 个商品在本地真源（本店快照 + 映射总表）中未收录"
              f"（未做联网核实）：")
        print("  " + ",".join(str(x) for x in sorted(agg["missing"])))
    if nmids:
        print("[WB商品码] nmId（英文逗号分隔，可直接复制）：")
        print(",".join(str(x) for x in sorted(nmids)))
    if vcs:
        print("[供应商代码] vendorCode（英文逗号分隔，可直接复制）：")
        print(",".join(sorted(vcs)))

    if rows:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.LOG_DIR, f"推广商品_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"[日志] {len(rows)} 行 → {path}")
    else:
        print("\n没有被推广商品（或全部活动均未下发商品明细）")
    return 0

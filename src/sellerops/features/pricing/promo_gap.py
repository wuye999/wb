"""promo gap。"""
from sellerops.features.pricing.promotion_types import SHOP_SLEEP
from sellerops.features.pricing.promotion_types import DEFAULT_MIN_ORDERS
from sellerops.features.pricing.promotion_types import DEFAULT_TABLE
from sellerops.features.pricing.promotion_types import ACTIVE_STATUS_ID


from sellerops.features.pricing.promotion_types import ALL_MODES


from sellerops.features.pricing.promotion_types import MODE_GAP
from sellerops.features.pricing.promotion_types import MODE_WASTE
from sellerops.features.pricing.promotion_types import MODE_BOTH
MODE_GAP, MODE_WASTE, MODE_BOTH = "gap", "waste", "both"


import time

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.settings import credentials
from sellerops.integrations.wildberries import advertising as ads_api
from sellerops.persistence.mappings import MappingRepository
from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.features.pricing import adverts as adverts_mod
from sellerops.features.pricing.promotion_report import _print_gap
from sellerops.features.pricing.promotion_report import _print_header
from sellerops.features.pricing.promotion_report import _print_waste
from sellerops.features.pricing.promotion_rules import build_gap
from sellerops.features.pricing.promotion_rules import build_waste
from sellerops.features.pricing.promotion_report import write_csv

def resolve_min(min_orders: Any, default: int = DEFAULT_MIN_ORDERS) -> int:
    """阈值解析：None/空串 → 默认值；非法或 < 1 → ValueError"""
    if min_orders is None or (isinstance(min_orders, str) and not str(min_orders).strip()):
        return default
    try:
        n = int(str(min_orders).strip())
    except (TypeError, ValueError):
        raise ValueError(f"--min 必须是正整数，实际收到: {min_orders!r}")
    if n < 1:
        raise ValueError(f"--min 必须 >= 1，实际收到 {n}")
    return n

def select_shops(all_shops: Sequence[Dict[str, Any]], tokens: Any = None
                 ) -> List[Tuple[Dict[str, Any], int, str]]:
    """凭证店铺 → [(shop_dict, shop_id, shop_name)]。

    `tokens` 支持 '9352' / '袁州1' / '9352,袁州3' / ['9352','袁州3']；空 = 全部。
    数字按 shopId 匹配，非数字按 shopName（店铺短名）匹配。
    """
    shops: List[Tuple[Dict[str, Any], int, str]] = []
    for s in (all_shops or []):
        shops.append((s, common.to_int(s.get("shopId") or s.get("shop_id")),
                      (s.get("shopName") or "").strip()))
    if not tokens:
        return shops
    if isinstance(tokens, str):
        items = [t.strip() for t in tokens.replace("，", ",").split(",") if t.strip()]
    elif isinstance(tokens, (list, tuple, set)):
        items = [str(t).strip() for t in tokens if str(t).strip()]
    else:
        items = [str(tokens).strip()]
    if not items:
        return shops
    want_ids: Set[int] = {int(t) for t in items if t.isdigit()}
    want_names: Set[str] = {t for t in items if not t.isdigit()}
    return [x for x in shops if x[1] in want_ids or (x[2] and x[2] in want_names)]

def load_shop_snapshot(shop_id: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """本店在架快照 {vc: row}；文件不存在返回 None"""
    return ProductSnapshotRepository.load_shop_rows(shop_id)

def collect_adverted(shop: Dict[str, Any], root_version: str, statuses: Any = None,
                     page_size: int = 100, max_pages: int = 50, limit: int = 0,
                     with_cn: bool = True) -> Dict[str, Any]:
    """本店「已被推广」明细 + 去重 vc 集合（同域直连 adverts 库入口）"""
    return adverts_mod.shop_adverted(shop, root_version, statuses=statuses, page_size=page_size,
                                     max_pages=max_pages, limit=limit, with_cn=with_cn)

def _empty_shop(sid: int, name: str, reason: str) -> Dict[str, Any]:
    """被跳过的店铺占位（保证返回结构一致、可核对）"""
    return {"shop_id": sid, "shop_name": name, "skip_reason": reason, "orders_rows": 0,
            "campaigns": 0, "total": -1, "truncated": False, "nm_unresolved": [],
            "missing_detail_campaigns": [], "adverted_total": 0}

def promo_gap(days: int = 7, begin: str = "", end: str = "", date: str = "",
              min_orders: Any = DEFAULT_MIN_ORDERS, shops: Any = None, statuses: Any = None,
              mode: str = MODE_GAP, include_not_listed: bool = True, with_cn: bool = True,
              url: str = "", table: str = DEFAULT_TABLE, page_size: int = 100,
              max_pages: int = 50, limit: int = 0, write_csv_file: bool = True) -> Dict[str, Any]:
    """★ 库入口：推广 × 销量双向错配审计（只读）。

    **销量判据 = 该 vc 在目标店铺（默认袁州1/2/3）的 7 天单数合计**，非单店单数。

    参数（全部显式关键字）：
      days/begin/end/date  时间窗口（默认近 7 天含今天；基准列 = 飞书「日期」= 付款时间）
      min_orders           阈值（默认 4）：gap = 合计 ≥ N；waste = 合计 < N
      shops                目标店铺（= 合计范围，也是推广侧审计范围）：'9352' / '袁州1' / ['9352','袁州3']
      statuses             推广活动状态（默认 (4,9,11)，含暂停）—— 仅影响「已推广」判定
      mode                 'gap' / 'waste' / 'both' —— **只影响打印与写文件**，返回里两个方向都在
      include_not_listed   gap 方向是否把「需先上架」组也写进 `gap_rows`
      with_cn              是否用本地映射真源补全缺失的中文名
      url/table            飞书表格地址与表名（默认读 credentials）
      page_size/max_pages/limit  推广活动分页参数

    返回 dict：{'begin','end','min_orders','statuses','active_status','table','mode','shop_labels',
                 'feishu_stats','shops':[...],'gap_rows','gap_listed','gap_unlisted_all',
                 'waste_rows','waste_paused','waste_unknown','waste_by_nm','waste_by_campaign',
                 'stats':{'gap','waste'},'warnings','csv_path'}
    """
    min_orders = resolve_min(min_orders)
    mode = (mode or MODE_GAP).strip().lower()
    if mode not in ALL_MODES:
        raise ValueError(f"mode 只能是 {'/'.join(ALL_MODES)}，实际收到: {mode!r}")
    statuses = tuple(statuses) if statuses else ads_api.STATUS_DEFAULT

    cred = credentials.get()
    all_shops = cred.wb_shops()
    if not all_shops:
        raise RuntimeError("credentials.json 中没有已填 cookie 的店铺")
    targets = select_shops(all_shops, shops)
    if not targets:
        raise RuntimeError(f"指定店铺 {shops!r} 未在凭证中找到已填 cookie 的店铺")
    labels = [name for _, _, name in targets]

    # 飞书只读一次；跨域只经 order_svc 门面（REUSE_GUIDE 铁律 3）
    from sellerops.features.orders import feishu_vc_stats as order_statistics
    fs = order_statistics.vc_order_stats(days=days, begin=begin, end=end, date=date,
                                        shops=None, url=url, table=table, with_cn=False)
    rows = fs["raw_rows"]
    begin, end = fs["begin"], fs["end"]
    resolve_cn = MappingRepository.build_vc_resolver()[0] if with_cn else None

    per_shop: List[Dict[str, Any]] = []
    warnings: List[str] = []
    adverted_by_shop: Dict[int, Set[str]] = {}
    snapshots: Dict[int, Optional[Dict[str, Dict[str, Any]]]] = {}
    adv_products: List[Dict[str, Any]] = []
    for shop, sid, name in targets:
        try:
            adv = collect_adverted(shop, cred.root_version, statuses, page_size=page_size,
                                   max_pages=max_pages, limit=limit, with_cn=with_cn)
        except common.CookieExpiredError as e:
            warnings.append(f"店铺 {name}: {e}（该店中止，继续下一店；cookie 失效请跑 sellerops cookies-update）")
            per_shop.append(_empty_shop(sid, name, "cookie_expired"))
            adverted_by_shop[sid] = set()
            snapshots[sid] = None
            time.sleep(SHOP_SLEEP)
            continue
        except Exception as e:
            warnings.append(f"店铺 {name} 处理失败: {e}（该店中止，继续下一店）")
            per_shop.append(_empty_shop(sid, name, "error"))
            adverted_by_shop[sid] = set()
            snapshots[sid] = None
            time.sleep(SHOP_SLEEP)
            continue

        adverted_by_shop[sid] = set(adv["vcs"].keys())
        snapshots[sid] = load_shop_snapshot(sid)
        for p in adv["products"]:
            adv_products.append({**p, "shop_id": sid, "shop_name": name})
        orders_rows = sum(1 for r in rows if name in (r.get("shops") or []))
        per_shop.append({
            "shop_id": sid, "shop_name": name, "skip_reason": "",
            "orders_rows": orders_rows, "campaigns": adv["campaign_count"], "total": adv["total"],
            "truncated": adv["truncated"], "nm_unresolved": adv["nm_unresolved"],
            "missing_detail_campaigns": adv["missing_detail_campaigns"],
            "adverted_total": len(adv["vcs"]),
        })

        if adv["no_snapshot"]:
            warnings.append(f"店铺 {name} 无 BCS 快照（{config.shop_json_path(sid)} 不存在）→ "
                            f"「是否在架」无法判断，候选商品全部归入「需先上架」；需最新请先跑 python -m sellerops fetch")
        if adv["truncated"]:
            warnings.append(f"店铺 {name} 推广活动分页未取全：平台口径 {adv['total']} 个，实取 "
                            f"{adv['campaign_count']} 个（会漏判「已推广」也会漏列该关的；建议加大 --page-size/--max-pages）")
        for cid in adv["missing_detail_campaigns"]:
            warnings.append(f"店铺 {name} 活动 {cid} 声明有商品但未下发明细（stocks.products 为空），其商品未计入")
        if adv["nm_unresolved"]:
            shown = ",".join(str(x) for x in adv["nm_unresolved"][:10])
            warnings.append(f"店铺 {name} 有 {len(adv['nm_unresolved'])} 个被推广 nmId 在本地真源未收录"
                            f"（正向可能「假缺口」/反向归入「无法判定」）：{shown}")
        time.sleep(SHOP_SLEEP)

    gap = build_gap(targets, rows, adverted_by_shop, snapshots, min_orders=min_orders,
                    resolve_cn=resolve_cn, with_cn=with_cn)
    waste = build_waste(targets, rows, adv_products, min_orders=min_orders, with_cn=with_cn)

    for st, sid, cid in waste.get("waste_unknown_status") or []:
        nm = next((s["shop_name"] for s in per_shop if s["shop_id"] == sid), str(sid))
        warnings.append(f"店铺 {nm} 活动 {cid} 出现未识别的活动状态 status={st}，已按「非在投」处理，请人工核对")
    for s in per_shop:
        if s["orders_rows"] == 0 and not s["skip_reason"]:
            warnings.append(f"店铺 {s['shop_name']} 在「{table}」窗口 {begin}~{end} 内 0 条记录 ⚠ "
                            f"反向结论会把该店全部在投商品判为「0 单该关」，请先核对店铺短名是否与表内一致"
                            f"（当前用「{s['shop_name']}」）")

    gap_rows = list(gap["gap_listed"]) + (list(gap["gap_unlisted"]) if include_not_listed else [])

    stats = {
        "gap": {
            "目标店铺": "、".join(labels),
            "跳过店铺数": sum(1 for s in per_shop if s["skip_reason"]),
            "飞书命中记录": len(rows),
            **gap["gap_stats"],
        },
        "waste": {
            "目标店铺": "、".join(labels),
            "被推广商品位": waste["waste_stats"]["被推广商品位"],
            "建议关闭": waste["waste_stats"]["建议关闭"],
            "已暂停无需操作": waste["waste_stats"]["已暂停无需操作"],
            "无法判定": waste["waste_stats"]["无法判定"],
            "涉及活动数": len({(x["shop_id"], x["campaign_id"]) for x in waste["waste_close"]}),
        },
    }

    csv_path = {"gap": "", "waste": "", "waste_campaigns": ""}
    if write_csv_file:
        if mode in (MODE_GAP, MODE_BOTH) and gap_rows:
            csv_path["gap"] = write_csv(gap_rows, "gap")
        if mode in (MODE_WASTE, MODE_BOTH) and waste["waste_close"]:
            csv_path["waste"] = write_csv(waste["waste_close"], "waste")
            csv_path["waste_campaigns"] = write_csv(waste["waste_by_campaign"], "campaign")

    return {
        "begin": begin, "end": end, "min_orders": min_orders, "statuses": list(statuses),
        "active_status": ACTIVE_STATUS_ID, "table": table, "mode": mode,
        "shop_labels": labels, "feishu_stats": fs["stats"],
        "shops": per_shop,
        "gap_rows": gap_rows, "gap_listed": gap["gap_listed"], "gap_unlisted_all": gap["gap_unlisted"],
        "waste_rows": waste["waste_close"], "waste_paused": waste["waste_paused"],
        "waste_unknown": waste["waste_unknown"],
        "waste_by_nm": waste["waste_by_nm"], "waste_by_campaign": waste["waste_by_campaign"],
        "stats": stats, "warnings": warnings, "csv_path": csv_path,
    }

def run(args: Any) -> int:
    """`sellerops promo-gap`：推广 × 销量双向错配审计（只读）。"""
    common.ensure_utf8_stdout()
    mode = (getattr(args, "mode", "") or MODE_GAP).strip().lower() or MODE_GAP
    top = common.to_int(getattr(args, "top", 0))
    listed_only = bool(getattr(args, "listed_only", False))
    if listed_only and mode == MODE_WASTE:
        print("[提示] --listed-only 仅对 gap 方向生效，waste 模式下已忽略")
    opt = adverts_mod.build_options(args)          # 复用 --status/--no-cn/分页参数解析（铁律 6）

    print(f"[开始] 方向={mode}｜阈值 --min {getattr(args, 'min', DEFAULT_MIN_ORDERS)}"
          f"｜正在读取飞书「订单登记」与各目标店推广活动（只读）…")
    try:
        r = promo_gap(days=getattr(args, "days", 7), begin=getattr(args, "begin", ""),
                      end=getattr(args, "end", ""), date=getattr(args, "date", ""),
                      min_orders=getattr(args, "min", DEFAULT_MIN_ORDERS),
                      shops=getattr(args, "shops", "") or "", statuses=opt["statuses"], mode=mode,
                      include_not_listed=not listed_only, with_cn=opt["with_cn"],
                      url=getattr(args, "url", ""),
                      table=(getattr(args, "table", "") or "").strip() or DEFAULT_TABLE,
                      page_size=opt["page_size"], max_pages=opt["max_pages"], limit=opt["limit"])
    except ValueError as e:
        print(f"[错误] {e}")
        return 1
    except RuntimeError as e:
        print(f"[错误] {str(e)[:300]}")
        return 1

    _print_header(r)
    if mode in (MODE_GAP, MODE_BOTH):
        _print_gap(r, top)
    if mode in (MODE_WASTE, MODE_BOTH):
        _print_waste(r, top)

    if r["warnings"]:
        print("\n[警告]")
        for w in r["warnings"]:
            print(f"  - {w}")
        print("[提示] 以上告警（未收录 nmId / 分页未取全 / 活动未下发明细）会让「已推广」集合偏小 → "
              "可能造成「假缺口」或漏列该关的，建议先跑 python -m sellerops fetch 后重试")

    if mode in (MODE_GAP, MODE_BOTH):
        if r["csv_path"]["gap"]:
            print(f"\n[日志] 缺口明细 {len(r['gap_rows'])} 行 → {r['csv_path']['gap']}")
        else:
            print("\n[提示] gap 方向无缺口（未写 CSV）")
    if mode in (MODE_WASTE, MODE_BOTH):
        if r["csv_path"]["waste"]:
            print(f"[日志] 待关明细 {len(r['waste_rows'])} 行 → {r['csv_path']['waste']}")
            print(f"[日志] 活动级汇总 {len(r['waste_by_campaign'])} 行 → {r['csv_path']['waste_campaigns']}")
        else:
            print("[提示] waste 方向无建议关闭项（未写 CSV）")
    return 0

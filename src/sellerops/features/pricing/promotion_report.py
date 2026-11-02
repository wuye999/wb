"""promotion report。"""
import csv
import os
import time

from typing import Any, Dict, Sequence

from sellerops.settings import paths as config


from sellerops.features.pricing.promotion_types import GAP_CSV_FIELDS
from sellerops.features.pricing.promotion_types import WASTE_CAMP_FIELDS
from sellerops.features.pricing.promotion_types import WASTE_CSV_FIELDS

def write_csv(rows: Sequence[Dict[str, Any]], kind: str = "gap") -> str:
    """写 CSV（utf-8-sig）；`kind` ∈ {'gap','waste','campaign'}；返回路径"""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    if kind == "gap":
        name, fields = f"推广缺口_{ts}.csv", GAP_CSV_FIELDS

        def row_of(x):
            return {"方向": "gap（该推没推）", "店铺": x["shop_name"], "店铺ID": x["shop_id"],
                    "分组": x["group"], "BCS编号": x["vc"], "商品中文名": x["cn"],
                    "三店合计单数": x["orders"], "该店单数": x["shop_orders"],
                    "各店单数拆分": x["shop_split"], "本店nmId": x["nm_id"], "备注": x["备注"]}
    elif kind == "waste":
        name, fields = f"推广待关_{ts}.csv", WASTE_CSV_FIELDS

        def row_of(x):
            return {"店铺": x["shop_name"], "店铺ID": x["shop_id"], "活动ID": x["campaign_id"],
                    "活动名": x["campaign_name"], "活动状态ID": x["status_id"],
                    "结算方式": x["payment_model"], "预算": x["budget"], "商品nmId": x["nm"],
                    "供应商代码": x["vc"], "商品中文名": x["cn"],
                    "三店合计单数": x["orders"], "该店单数": x["shop_orders"],
                    "各店单数拆分": x["shop_split"],
                    "该nm本店被推广活动数": x["n_campaigns"],
                    "判定": x["verdict"], "备注": x["备注"]}
    elif kind == "campaign":
        name, fields = f"推广待关_按活动汇总_{ts}.csv", WASTE_CAMP_FIELDS

        def row_of(x):
            return {k: x.get(k) for k in fields}
    else:
        raise ValueError(f"未知的 CSV 类型: {kind!r}")

    path = os.path.join(config.LOG_DIR, name)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in rows:
            w.writerow(row_of(x))
    return path

def _print_header(r: Dict[str, Any]) -> None:
    label = {"gap": "gap（该推没推）", "waste": "waste（在推广但合计单数<阈值 → 建议关闭）",
             "both": "gap + waste（双向）"}[r["mode"]]
    print(f"[区间] {r['begin']} ~ {r['end']}（闭区间；基准列=日期/付款时间）｜方向={label}"
          f"｜阈值 --min {r['min_orders']}")
    print(f"       销量判据 = 「{'、'.join(r['shop_labels'])}」的**合计单数**（非单店）"
          f"｜「已推广」口径=status{r['statuses']}（含暂停）"
          f"｜反向「建议关闭」判定=在投 status{r['active_status']}")
    print("[明细] 飞书未命中原因（显式标注）: " +
          (" / ".join(f"{k}={v}" for k, v in (r["feishu_stats"] or {}).items()
                      if k != "总记录" and v) or "无"))

def _print_gap(r: Dict[str, Any], top: int) -> None:
    st = r["stats"]["gap"]
    print(f"\n[汇总·gap] 飞书命中 {st['飞书命中记录']} 条记录｜合计单数≥{r['min_orders']} 的供应商代码 "
          f"{st['候选vc数']} 个 → 缺口店铺位 {st['候选店铺位']} 个"
          f"（可直接补推 {st['可直接补推']} / 需先上架 {st['需先上架']}）｜跳过店铺 {st['跳过店铺数']} 个")
    if not r["gap_listed"] and not r["gap_unlisted_all"]:
        print("  [提示] 无缺口（合计达标的供应商代码在所有目标店都已推广）")
        return
    for group, title, note in ((r["gap_listed"], "组① 可直接补推（该店在架）", None),
                               (r["gap_unlisted_all"], "组② 需先上架（该店快照查不到该 vc）",
                                "⚠ 飞书「店铺」= 下单店铺，本组仅表示该店快照未收录该 vc，需人工确认")):
        if not group:
            continue
        print(f"  # {title}")
        if note:
            print(f"  {note}")
        print("  排名 | 店铺 | BCS编号 | 商品中文名 | 合计单数 | 该店单数 | 各店拆分 | 本店nmId | 备注")
        for i, x in enumerate(group[:top] if top else group, 1):
            print(f"  {i:>4} | {x['shop_name']} | {x['vc']} | {x['cn'] or '-'} | {x['orders']} | "
                  f"{x['shop_orders']} | {x['shop_split'] or '-'} | "
                  f"{x.get('nm_id') or '-'} | {x['备注'] or '-'}")
        if top and len(group) > top:
            print(f"  ... 该组其余 {len(group) - top} 个见 CSV")
    listed = [x["vc"] for x in r["gap_listed"]]
    if listed:
        print("  [补推vc] " + ",".join(dict.fromkeys(listed)))

def _print_waste(r: Dict[str, Any], top: int) -> None:
    st = r["stats"]["waste"]
    print(f"\n[汇总·waste] 被推广商品位 {st['被推广商品位']} 个 → 建议关闭 {st['建议关闭']} 个"
          f"（涉及 {st['涉及活动数']} 个活动）｜已暂停无需操作 {st['已暂停无需操作']} 个"
          f"｜无法判定 {st['无法判定']} 个")
    print(f"  [口径] 判据 = 「{'、'.join(r['shop_labels'])}」**合计单数 < {r['min_orders']}**"
          f"；只列「在投(status={r['active_status']})」活动，暂停活动已停无需操作")
    print("  ⚠ [风险] 商品在本店刚上架/尚未起量时也会进清单，请结合上架时间人工判断")
    if not r["waste_rows"]:
        print("  [提示] 无建议关闭项")
    else:
        print("  店铺 | 活动ID | 活动名 | 结算 | 预算 | 商品nmId | 供应商代码 | 商品中文名 | 合计/该店 | 各店拆分 | 备注")
        for x in (r["waste_rows"][:top] if top else r["waste_rows"]):
            print(f"  {x['shop_name']} | {x['campaign_id']} | {x['campaign_name'] or '-'} | "
                  f"{x['payment_model']} | {x['budget']} | {x['nm']} | {x['vc'] or '-'} | "
                  f"{x['cn'] or '-'} | {x['orders']}/{x['shop_orders']} | {x['shop_split'] or '-'} | {x['备注']}")
        if top and len(r["waste_rows"]) > top:
            print(f"  ... 其余 {len(r['waste_rows']) - top} 个见 CSV")
    if r["waste_paused"]:
        print(f"  # 已暂停无需操作 —— {len(r['waste_paused'])} 个（活动已在停，不逐条列）")
    if r["waste_unknown"]:
        print(f"  # 无法判定（nm 本地真源未收录，无法关联销量）—— {len(r['waste_unknown'])} 个，需人工判断")
        print("  店铺 | 活动ID | 商品nmId | 俄文标题 | 备注")
        for x in r["waste_unknown"]:
            print(f"  {x['shop_name']} | {x['campaign_id']} | {x['nm']} | {(x['ru'] or '-')[:36]} | {x['备注']}")
    if r["waste_by_campaign"]:
        print("  # 活动级汇总（便于整活动关闭）")
        print("  店铺 | 活动ID | 活动名 | 状态 | 被推广商品数 | 建议关闭数 | 结论")
        for x in r["waste_by_campaign"]:
            print(f"  {x['shop_name']} | {x['campaign_id']} | {x['campaign_name'] or '-'} | "
                  f"{x['status_id']} | {x['被推广商品数']} | {x['建议关闭数']} | "
                  f"{x['是否整活动建议关闭'] or '-'}")
    if r["waste_by_nm"]:
        print("  # 商品级汇总（该 nm 在本店出现在几个活动）")
        print("  店铺 | 商品nmId | 供应商代码 | 商品中文名 | 合计单数 | 该店单数 | 本店活动数")
        for x in r["waste_by_nm"]:
            print(f"  {x['shop_name']} | {x['nm']} | {x['vc'] or '-'} | {x['cn'] or '-'} | "
                  f"{x['orders']} | {x['shop_orders']} | {x['n_campaigns']}")

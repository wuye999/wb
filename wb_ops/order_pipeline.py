# -*- coding: utf-8 -*-
"""
wb_ops 新订单全链路编排（orders-pipeline）

固化顺序（用户规则）：新订单**先登记飞书**，再执行后续流程（处理后订单离开
待处理页进入全部订单，不便于筛查）：
  ① 登记飞书「爆品登记」（按订单编号去重）        → 报告: 飞书订单登记_*.csv
  ② 商品匹配更换（VC→映射表中文名→库存SKU）        → 报告: 马帮订单匹配_*.csv
  ③ 生成预报批次→上传→等待→物流交运「七库海外仓」  → 报告: 马帮预报批次_*.csv
  ④ 归属统计：店铺 × 商品中文名 订单数汇总         → 报告: 新订单归属统计_*.csv
每一步幂等可重跑；某步失败即停止后续步骤。
"""
import csv
import os
import time
from types import SimpleNamespace

from . import config
from . import common
from . import mabang
from . import feishu_register

STATS_FIELDS = ["店铺", "商品中文名", "订单数"]


def stats_report(registered):
    """归属统计：店铺 × 商品中文名 → 订单数；打印 + 写 CSV，返回 csv 路径"""
    agg = {}
    for r in registered:
        shop = (r.get("店铺") or ["-"])[0] if r.get("店铺") else "-"
        cn = r.get("商品中文名") or f"(未匹配 {r.get('BCS编号') or '无BCS'})"
        agg[(shop, cn)] = agg.get((shop, cn), 0) + 1

    print(f"\n{'=' * 72}")
    print(f"[归属统计] 本次新登记 {len(registered)} 单")
    print(f"{'=' * 72}")
    print(f"{'店铺':<28} {'商品中文名':<16} {'订单数':>4}")
    print("-" * 72)
    rows = []
    for (shop, cn), n in sorted(agg.items(), key=lambda x: (x[0][0], -x[1])):
        print(f"{shop:<28} {cn:<16} {n:>4}")
        rows.append({"店铺": shop, "商品中文名": cn, "订单数": n})
    total = sum(r["订单数"] for r in rows)
    print("-" * 72)
    print(f"{'合计':<46} {total:>4}（{len(rows)} 个组合）")

    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"新订单归属统计_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=STATS_FIELDS)
        w.writeheader()
        w.writerows(rows)
        w.writerow({"店铺": "合计", "商品中文名": f"{len(rows)} 个组合", "订单数": total})
    print(f"[汇总] 统计明细: {path}")
    return path


def run(args):
    common.ensure_utf8_stdout()
    if not args.url or not args.table:
        print("[错误] 必须提供 --url 表格地址 与 --table 表格名")
        return 1

    t0 = time.time()
    # ① 商品匹配更换（先匹配：登记飞书的库存SKU 才是更换后的正确值）
    print("\n" + "#" * 72)
    print("# 步骤 ① 新订单商品匹配更换（VC→中文名→库存SKU）")
    print("#" * 72)
    ns2 = SimpleNamespace(days=args.days, page_size=args.page_size, apply=True, sync=False)
    code = mabang.run(ns2)
    if code:
        print("[中止] 商品匹配更换失败，后续步骤不执行")
        return code

    # ② 登记飞书（匹配完成后登记，库存SKU=马帮实际匹配值）
    ns = SimpleNamespace(url=args.url, table=args.table, days=args.days,
                         page_size=args.page_size, scope="pending",
                         date="", begin="", end="", apply=True,
                         registered_new=[])
    print("\n" + "#" * 72)
    print("# 步骤 ② 新订单登记飞书（匹配完成后，库存SKU 为实际匹配值）")
    print("#" * 72)
    code = feishu_register.run(ns)
    if code:
        print("[中止] 登记飞书失败，后续步骤不执行")
        return code
    registered = list(getattr(ns, "registered_new", []))

    # ③ 预报批次→上传→等待→交运
    print("\n" + "#" * 72)
    print("# 步骤 ③ 预报批次生成/上传/物流交运（幂等跳过已完成项）")
    print("#" * 72)
    ns3 = SimpleNamespace(days=args.days, page_size=args.page_size, apply=True,
                          check=False, wait=0, upload_waiting=False)
    code = mabang.run_forecast(ns3)
    if code:
        print("[中止] 预报/交运失败")
        return code

    # ④ 归属统计
    print("\n" + "#" * 72)
    print("# 步骤 ④ 新订单商品中文名归属统计")
    print("#" * 72)
    if registered:
        stats_report(registered)
    else:
        print("[统计] 本次无新登记订单（可能全部已登记），无归属统计")

    print(f"\n[完成] 全链路耗时 {time.time() - t0:.0f}s；"
          f"新登记 {len(registered)} 单；各步骤明细见 data/logs/ 对应 CSV")
    return 0

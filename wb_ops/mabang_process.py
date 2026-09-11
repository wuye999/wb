# -*- coding: utf-8 -*-
"""
wb_ops 马帮订单处理一体脚本：
匹配商品 → 生成预报单 → 依次上传预报单（自动发货）→ 物流交运 → 自动登记飞书
过滤机制与既有流程一致：shop_map 店铺过滤 / 取消单排除（WB 门户）/
NO_SKU（价格表缺库存SKU）只匹配不预报 / 幂等跳过（已预报、已交运）
飞书登记：数据源=orderalllist 最近 500 条全状态订单，按订单编号去重只登新增；
URL 从 credentials.json 的 feishu.base_url 读取（--url 可覆盖）
"""
import time
from types import SimpleNamespace

from . import common
from . import credentials
from . import feishu_register
from . import mabang


def run(args):
    common.ensure_utf8_stdout()
    t0 = time.time()

    # ① 匹配商品（强制更换为价格表库存SKU）
    print("\n" + "#" * 72)
    print("# 步骤 ① 马帮订单商品匹配更换（VC→中文名→库存SKU）")
    print("#" * 72)
    ns_match = SimpleNamespace(days=args.days, page_size=args.page_size,
                               apply=args.apply, sync=False)
    code = mabang.run(ns_match)
    if code:
        print("[中止] 商品匹配失败，后续步骤不执行")
        return code

    # ② 生成预报单 → 依次上传（自动发货）→ 物流交运
    print("\n" + "#" * 72)
    print("# 步骤 ② 预报单生成/上传/物流交运（幂等跳过已完成项）")
    print("#" * 72)
    ns_fc = SimpleNamespace(days=args.days, page_size=args.page_size,
                            apply=args.apply, check=False,
                            wait=args.wait, upload_waiting=False)
    code = mabang.run_forecast(ns_fc)
    if code:
        print("[中止] 预报/交运失败")
        return code

    # ③ 自动登记飞书（匹配完成后登记，库存SKU 为实际匹配值）
    print("\n" + "#" * 72)
    print("# 步骤 ③ 新订单登记飞书（最近500条去重，只登新增）")
    print("#" * 72)
    url = args.url or credentials.get().feishu_base_url()
    if not url:
        print("[提示] 未提供 --url 且配置 feishu.base_url 缺失，跳过飞书登记")
        print(f"\n[完成] 马帮订单处理耗时 {time.time() - t0:.0f}s")
        return 0
    ns_reg = SimpleNamespace(url=url, table=args.table, days=args.days,
                             page_size=args.page_size, scope="latest",
                             date="", begin="", end="", apply=args.apply,
                             registered_new=[])
    try:
        code = feishu_register.run(ns_reg)
        if code:
            print("[警告] 飞书登记失败（马帮处理已完成，可单独重跑 feishu-register 补登记）")
    except Exception as e:
        print(f"[警告] 飞书登记异常（马帮处理已完成）: {str(e)[:120]}")

    print(f"\n[完成] 马帮订单处理+飞书登记耗时 {time.time() - t0:.0f}s")
    return 0

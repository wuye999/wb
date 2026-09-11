# -*- coding: utf-8 -*-
"""
wb_ops 马帮订单处理一体脚本（零飞书依赖）：
匹配商品 → 生成预报单 → 依次上传预报单（自动发货）→ 物流交运
过滤机制与既有流程一致：shop_map 店铺过滤 / 取消单排除（WB 门户）/
NO_SKU（价格表缺库存SKU）只匹配不预报 / 幂等跳过（已预报、已交运）
"""
import time
from types import SimpleNamespace

from . import common
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

    print(f"\n[完成] 马帮订单处理耗时 {time.time() - t0:.0f}s（本脚本不涉及飞书，"
          "登记请用 wb.py feishu-register）")
    return 0

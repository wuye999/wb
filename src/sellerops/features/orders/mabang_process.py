"""处理新增订单，按发货方式分流并执行匹配、预报、交运及飞书登记。"""
import time

from sellerops.core import utils as common
from sellerops.settings import credentials
from sellerops.features.orders.feishu_register import run as feishu_register_run
from sellerops.features.orders.matching import match_orders
from sellerops.features.orders.forecast import run_forecast as mabang_run_forecast
from sellerops.integrations.feishu.tables import find_lark_cli


def run(args):
    common.ensure_utf8_stdout()
    t0 = time.time()

    # ① 发货分流并匹配马帮库存 SKU
    print("\n" + "#" * 72)
    print("# 步骤 ① 按发货方式分流并更换马帮库存SKU")
    print("#" * 72)
    matched = match_orders(days=args.days, page_size=args.page_size, apply=args.apply)
    if matched.stopped:
        print("[中止] 商品匹配中止，后续步骤不执行")
        return 1
    replace_fail_ids = matched.failed_ids
    group_shipping_ids = matched.group_ids
    if replace_fail_ids:
        print(f"\n[提示] 步骤① 未完成商品匹配或更换 {len(replace_fail_ids)} 单："
              "本轮不进预报/交运")

    # ② 生成预报单 → 依次上传（自动发货）→ 物流交运
    print("\n" + "#" * 72)
    print("# 步骤 ② 预报单生成/上传/物流交运（幂等跳过已完成项）")
    print("#" * 72)
    code = mabang_run_forecast(args, failed_ids=matched.failed_ids, group_shipping_ids=matched.group_ids, matched_ids=matched.matched_ids)
    if code:
        print("[中止] 预报/交运失败")
        return code

    # ③ 自动登记飞书
    print("\n" + "#" * 72)
    print("# 步骤 ③ 新增订单飞书登记（按订单编号去重）")
    print("#" * 72)
    url = args.url.strip() or credentials.get().feishu_base_url().strip()
    fail_tail = (f"；匹配或更换失败 {len(replace_fail_ids)} 单未预报/交运"
                 if replace_fail_ids else "")
    skip_reason = ""
    if not url:
        skip_reason = "未提供 --url 且配置 feishu.base_url 缺失"
    elif not find_lark_cli():
        skip_reason = "未检测到 lark-cli"
    if skip_reason:
        print(f"[提示] {skip_reason}，跳过飞书登记")
        print(f"\n[完成] 处理新增订单耗时 {time.time() - t0:.0f}s{fail_tail}")
        return 1 if replace_fail_ids else 0
    args.url = url
    registration_failed = False
    try:
        code = feishu_register_run(args, group_shipping_ids=group_shipping_ids)
        if code:
            registration_failed = True
            print("[警告] 飞书登记失败，本轮预报与交运步骤已结束。可单独运行 order register 补登记。")
    except Exception as e:
        registration_failed = True
        print("[警告] 飞书登记异常，本轮预报与交运步骤已结束。可单独运行 order register 补登记。"
              f" 异常摘要 {str(e)[:120]}")

    print(f"\n[完成] 处理新增订单及飞书登记耗时 {time.time() - t0:.0f}s{fail_tail}")
    return 1 if registration_failed or replace_fail_ids else 0

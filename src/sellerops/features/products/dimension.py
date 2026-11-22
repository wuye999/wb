"""
sellerops 批量修改尺寸（dimension）

按商品价格表「尺寸」列（长*宽*高/毛重）统一修改各店商品尺寸；也可用 --dims 自定义尺寸/毛重：
- 数据源：商品价格表尺寸 → 映射表 state(vc→中文名) → 各店快照 nmId；或 --dims 直接指定（统一值，配 --vc/--name/--prefix 圈定）。
- 接口：POST {base}/shopKeeper/dimension/batch，尺寸/毛重数值原样透传（长宽高 cm、毛重 kg，可为小数）。

用法：sellerops product dimensions [--vc|--prefix|--name] [--shops] [--limit] [--dims '长*宽*高/毛重'] [--apply]
安全：默认 dry-run；--apply 执行；提交后不验证、不同步、不合并。
"""
import csv
from itertools import islice
from math import isfinite
import os
import re
import time
from datetime import datetime
from sellerops.features.catalog.selection import selected_shops, resolve_filters, load_state, load_shop_rows

from sellerops.integrations.bcs.client import client as bcs
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.features.products.plans import prepare_cards, report_plan_skips, shop_targets
from sellerops.features.products.product_data import ProductDataResolver
from sellerops.domain.snapshot_values import positive_id

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

CHUNK = 300
SHOP_SLEEP = 0.6
BATCH_SLEEP = 0.15

_DIM_RE = re.compile(r"^([\d.]+)\s*\*\s*([\d.]+)\s*\*\s*([\d.]+)\s*/\s*([\d.]+)$")


def _parse_dims(value) -> tuple[float, float, float, float] | None:
    match = _DIM_RE.fullmatch(str(value or "").strip())
    if match:
        try:
            length, width, height, weight = map(float, match.groups())
        except ValueError:
            return None
        if all(isfinite(number) and number > 0 for number in (length, width, height, weight)):
            return length, width, height, weight
    return None


def build_plans(vcs, shops, state, dims=None, *, resolver=None, shop_rows=None, limit=0):
    """优先使用显式尺寸；按表时使用完整 VC 对应的价格表行。"""
    plans = []
    for sid in shops:
        rows = shop_rows[sid] if shop_rows is not None else load_shop_rows(sid)
        plan, targets = shop_targets(sid, vcs, rows, state)
        plans.append(plan)
        items, blocked = plan["items"], plan["blocked_items"]
        packages, eligible = {}, {}
        for vc, row in islice(targets.items(), limit or None):
            identity = state.get(vc, {})
            package = dims or _parse_dims((identity.get("price_row") or {}).get("dimensions"))
            if not package:
                blocked.append({"vc": vc, "cn": identity.get("cn", ""), "reason": "对应价格表行缺少有效尺寸"})
                continue
            packages[vc] = package
            eligible[vc] = row
        for vc, row in prepare_cards(sid, eligible, rows, state, blocked, resolver).items():
            length, width, height, weight = packages[vc]
            items.append({"vc": vc, "cn": state.get(vc, {}).get("cn", ""), "nmId": positive_id(row["nmId"]),
                          "length": length, "width": width, "height": height,
                          "weightBrutto": weight, "src": "自定义" if dims else "价格表"})
        if resolver:
            resolver.block_after_auth_failure(sid, items, blocked)
    report_plan_skips(plans, "改尺寸")
    return plans


def dry_run(plans):
    print(f"\n{YELLOW}===== 计划清单（dry-run，未执行）====={RESET}")
    total = 0
    for p in plans:
        print(f"\n店{p['shopId']}（{len(p['items'])} 个商品）:")
        for it in p["items"]:
            print(f"  {it['vc']} | {it['cn']} | nmId={it['nmId']} | "
                  f"{it['src']} {it['length']}*{it['width']}*{it['height']}cm / {it['weightBrutto']}kg")
            total += 1
        for item in p.get('blocked_items', []):
            print(f"  [已阻止] {item['vc']} | {item.get('cn', '')} | {item['reason']}")
    print(f"\n合计 {total} 条操作（{len(plans)} 个店铺）。加 --apply 执行。")


def _post(body, label):
    try:
        r = bcs.post(f"{bcs.base_url}/shopKeeper/dimension/batch", body)
        msg = r.get("msg", "")
        if r.get("code") == 200:
            print(f"  {GREEN}✓{RESET} {label} 成功: {msg}")
            return True, (msg or "OK")
        print(f"  {RED}✗{RESET} {label} 失败: code={r.get('code')} {msg}")
        return False, f"FAIL: {msg}"
    except Exception as e:
        print(f"  {RED}✗{RESET} {label} 异常: {e}")
        return False, f"FAIL: {e}"


def apply_plans(plans):
    """逐店分块 ≤300 POST；code==200 记成功，失败记原因。返回 (ok, fail, results)。"""
    ok = fail = 0
    results = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for pi, p in enumerate(plans):
        sid = p["shopId"]
        for item in p.get('blocked_items', []):
            fail += 1
            print(f"  [已阻止] 店{sid} {item['vc']}: {item['reason']}")
            results.append([ts, sid, item['vc'], item.get('cn', ''), '', '', '', '', '', '跳过', item['reason']])
        dl = [{"nmId": it["nmId"], "length": it["length"], "width": it["width"],
               "height": it["height"], "weightBrutto": it["weightBrutto"], "shopId": sid} for it in p["items"]]
        num = 0
        for i in range(0, len(dl), CHUNK):
            chunk = dl[i:i + CHUNK]
            batch_items = p["items"][i:i + CHUNK]
            body = {"dataList": chunk}
            ok_flag, msg = _post(body, f"dimension 批{i // CHUNK + 1}")
            if ok_flag:
                ok += len(chunk)
            else:
                fail += len(chunk)
            for it in batch_items:
                results.append([ts, sid, it["vc"], it["cn"], it["nmId"],
                                it["length"], it["width"], it["height"],
                                it["weightBrutto"], "成功" if ok_flag else "失败",
                                "" if ok_flag else msg])
            num += len(chunk)
            if i + CHUNK < len(dl):
                time.sleep(BATCH_SLEEP)
        print(f">>> 店{sid}：dimension 提交 {num} 条")
        if pi < len(plans) - 1:
            time.sleep(SHOP_SLEEP)
    return ok, fail, results


def run(args):
    dims = _parse_dims(args.dims)
    if args.dims and dims is None:
        raise ValueError("--dims 应为有效的 长*宽*高/毛重，例如 8*14*26/0.3")
    shops = selected_shops(args)
    state, shop_rows, boss = load_state(shops, all_products=getattr(args, "all_products", False))

    vcs, _ = resolve_filters(args, state, boss)
    limit = getattr(args, "limit", 0)
    if limit < 0:
        raise ValueError("每店数量上限不能为负数")

    with ProductDataResolver() as resolver:
        plans = build_plans(vcs, shops, state, dims=dims, resolver=resolver, shop_rows=shop_rows, limit=limit)
    incomplete = any(plan.get("range_error") or plan["blocked_items"] for plan in plans)
    if not args.apply:
        dry_run(plans)
        return int(incomplete)
    if not any(plan["items"] or plan["blocked_items"] for plan in plans):
        return int(incomplete)

    ok, fail, results = apply_plans(plans)

    csv_path = os.path.join(config.current.log_dir, f"尺寸修改_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(config.current.log_dir, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "店铺", "vc", "中文名", "nmId", "长", "宽", "高", "毛重", "结果", "原因"])
        w.writerows(results)
    print(f"\n结果: 成功 {ok} · 失败 {fail}（明细已保存 {csv_path}）")

    common.print_write_hint()
    return int(incomplete or fail > 0)

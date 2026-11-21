"""
sellerops 批量修改尺寸（dimension）

按商品价格表「尺寸」列（长*宽*高/毛重）统一修改各店商品尺寸；也可用 --dims 自定义尺寸/毛重：
- 数据源：商品价格表尺寸 → 映射表 state(vc→中文名) → 各店快照 nmId；或 --dims 直接指定（统一值，配 --vc/--name/--prefix 圈定）。
- 接口：POST {base}/shopKeeper/dimension/batch，尺寸/毛重数值原样透传（长宽高 cm、毛重 kg，可为小数）。

用法：sellerops product dimensions [--vc|--prefix|--name] [--shops] [--limit] [--dims '长*宽*高/毛重'] [--apply]
安全：默认 dry-run；--apply 执行；提交后不验证、不同步、不合并。
"""
from sellerops.features.products.skip_report import SkipTracker


import csv
import os
import re
import sys
import time
from datetime import datetime
from sellerops.features.catalog.selection import selected_shops, resolve_filters, load_state, load_shop_rows

from sellerops.integrations.bcs.client import client as bcs
from sellerops.core import utils as common
from sellerops.settings import paths as config
from sellerops.features.products.plans import report_blocked
from sellerops.features.products.product_data import ProductDataResolver
from sellerops.domain.snapshot_values import positive_id

from sellerops.features.publishing.import_publish import price_book_packages

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

CHUNK = 300
SHOP_SLEEP = 0.6
BATCH_SLEEP = 0.15

_DIM_RE = re.compile(r"^([\d.]+)\s*\*\s*([\d.]+)\s*\*\s*([\d.]+)\s*/\s*([\d.]+)$")


def _parse_dims(s):
    """解析 --dims：'长*宽*高/毛重' → (L,W,H,wt)；空串返回 None；非法格式报错退出。"""
    if not s or not s.strip():
        return None
    m = _DIM_RE.match(s.strip())
    if not m:
        print(f"[错误] --dims 格式非法：'{s}'，应为 '长*宽*高/毛重'（例 8*14*26/0.3）")
        sys.exit(1)
    return (float(m.group(1)), float(m.group(2)),
            float(m.group(3)), float(m.group(4)))


def build_plans(vcs, shops, state, pkg, dims=None, *, resolver=None, shop_rows=None, limit=0):
    """逐店组装改尺寸计划 + 跳过统计。dims=(L,W,H,wt) 时优先用它，不再查价格表。"""
    plans = []
    tracker = SkipTracker()
    for sid in shops:
        rows = shop_rows[sid] if shop_rows is not None else load_shop_rows(sid)
        if rows is None:
            plans.append({'shopId': sid, 'items': [], 'blocked_items': [
                {'vc': vc, 'cn': state.get(vc, {}).get('cn', ''), 'reason': '本店快照缺失或不可读，无法定位商品'}
                for vc in dict.fromkeys(vcs)]})
            continue
        items, blocked = [], []
        considered = 0
        for vc in dict.fromkeys(vcs):
            r = rows.get(vc)
            if not r:
                tracker.add(sid, vc, "该店无此商品（未在架或未同步）")
                continue
            if limit and considered >= limit:
                break
            considered += 1
            cn = state.get(vc, {}).get('cn', '')
            if resolver:
                prepared = resolver.ensure(sid, vc, r)
                if prepared.reason:
                    blocked.append({'vc': vc, 'cn': cn, 'reason': prepared.reason})
                    continue
                r = prepared.row
            nm_id = positive_id(r.get("nmId"))
            if nm_id is None:
                blocked.append({'vc': vc, 'cn': cn, 'reason': '缺少有效nmId'})
                continue
            if dims is not None:
                L, W, H, wt = dims
                src = "自定义"
            else:
                p = pkg.get(cn)
                if not p:
                    blocked.append({'vc': vc, 'cn': cn, 'reason': f"中文名「{cn or '-'}」无商品价格表尺寸"})
                    continue
                L, W, H, wt = p
                src = "价格表"
            items.append({"vc": vc, "cn": cn, "nmId": nm_id,
                          "length": L, "width": W, "height": H,
                          "weightBrutto": wt, "src": src})
        if resolver:
            resolver.block_after_auth_failure(sid, items, blocked)
        if items or blocked:
            plans.append({"shopId": sid, "items": items, 'blocked_items': blocked})
    report_blocked(plans, tracker, f"改尺寸 {len(vcs)} 个 vendorCode × {len(shops)} 店")
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
    dims = _parse_dims(getattr(args, "dims", "") or "")
    pkg = price_book_packages(config.current) if dims is None else {}
    if dims is not None and not (getattr(args, "vc", "") or getattr(args, "name", "") or getattr(args, "name_contains", "") or getattr(args, "prefix", "") or getattr(args, "sku", "") or getattr(args, "all_products", False)):
        print("[错误] 使用 --dims 时必须用 --vc / --name / --prefix 至少其一圈定目标，防止误改全部商品")
        sys.exit(1)
    if not pkg and dims is None:
        print("[错误] 商品价格表无可用尺寸数据（请确认「尺寸」列格式为 长*宽*高/毛重）")
        sys.exit(1)
    shops = selected_shops(args)
    state, shop_rows, boss = load_state(shops, all_products=getattr(args, "all_products", False))

    vcs, _ = resolve_filters(args, state, boss)
    if not vcs:
        print("[提示] 筛选结果为空，无操作")
        return 0
    limit = getattr(args, "limit", 0)
    if limit < 0:
        raise ValueError("每店数量上限不能为负数")

    with ProductDataResolver() as resolver:
        plans = build_plans(vcs, shops, state, pkg, dims=dims, resolver=resolver, shop_rows=shop_rows, limit=limit)
    if not plans:
        print(f"\n{RED}⚠ [无任何可执行项]{RESET} 目标商品在各店铺数据中均无法改尺寸（上方 [跳过] 已列明原因）")
        print("  可能原因：未先 fetch 最新数据 / 商品已下架 / nmId 为空 / 中文名无商品价格表尺寸。")
        return 0

    if not args.apply:
        dry_run(plans)
        return 1 if any(p.get('blocked_items') for p in plans) else 0

    ok, fail, results = apply_plans(plans)

    csv_path = os.path.join(config.current.log_dir, f"尺寸修改_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(config.current.log_dir, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "店铺", "vc", "中文名", "nmId", "长", "宽", "高", "毛重", "结果", "原因"])
        w.writerows(results)
    print(f"\n结果: 成功 {ok} · 失败 {fail}（明细已保存 {csv_path}）")

    common.print_write_hint()
    return 1 if fail else 0

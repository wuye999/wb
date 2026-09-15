# -*- coding: utf-8 -*-
"""
wb_ops WB 原生批量改折扣（discount / discount-wb）

全面采用 WB 原生接口进行从高到低查询与分片批量修改：
1. 列表接口：POST https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/list/goods/filter
   - 排序参数：sort="discount", sortOrder=0（从高到低）
   - 降序提前截断：首条 <= threshold 即停止分页
2. 批量修改接口：POST https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers/api/v1/upload/task?checkChange=true
   - 数据格式：{"data": [{"vendorCode": ..., "nmID": ..., "discount": target, "currencyIsoCode": "CNY"}, ...]}

灵活性与高级筛选支持：
- --vc: 单个 VC 或逗号分隔多个 VC（如 --vc BCS-XXX-1,BCS-XXX-2）
- --name: 商品中文名包含匹配（如 --name 笔记本电脑）
- --prefix: vendorCode 前缀码包含匹配（如 --prefix DSGI）
- --shops: 限定店铺 ID 逗号分隔（如 --shops 9352,9353）
- --threshold: 折扣阈值（默认 50，仅处理折扣 > 该值的商品）
- --all: 忽略折扣阈值，处理指定条件下的全部在架商品（等价于 --threshold -1）
- --target: 目标折扣（默认 50）
- --limit: 每店最多处理 N 条（0=不限）
- --chunk: 每批提交条数（默认 100，最大 300）
- --verify: 执行后验证（默认关闭，因 WB 异步生效延迟）
- --apply: 真正提交修改（默认 dry-run 预览）
"""
import argparse
import csv
import json
import os
import re
import sys
import time

from . import common
from . import config
from . import credentials
from . import mapping
from . import ops
from . import wb_api

DISC_LIST = (
    "https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers"
    "/api/v1/list/goods/filter"
)
DISC_UPLOAD = (
    "https://discounts-prices.wildberries.ru/ns/dp-api/discounts-prices/suppliers"
    "/api/v1/upload/task?checkChange=true"
)

PAGE_SIZE_DEF = 100
MAX_PAGES = 200
CHUNK_DEF = 100
CHUNK_SLEEP = 0.3
SHOP_SLEEP = 0.6
VERIFY_WAIT = 5


def build_vc_resolver():
    """读取全局 VC 归属池、人工纠偏池与前缀映射，返回 (resolve_cn, vc_cn_dict)。"""
    vc_cn = {}
    # 1. vc_known.json
    if os.path.exists(config.VC_KNOWN_JSON):
        try:
            with open(config.VC_KNOWN_JSON, encoding="utf-8") as f:
                for vc, info in json.load(f).items():
                    if isinstance(info, dict) and info.get("cn"):
                        vc_cn[vc.upper()] = info["cn"]
        except Exception:
            pass

    # 2. vc_override.json (更高优先级)
    if os.path.exists(config.VC_OVERRIDE_JSON):
        try:
            with open(config.VC_OVERRIDE_JSON, encoding="utf-8") as f:
                for vc, info in json.load(f).items():
                    if isinstance(info, dict) and info.get("cn"):
                        vc_cn[vc.upper()] = info["cn"]
        except Exception:
            pass

    # 3. 前缀映射兜底
    prefix_map = {}
    try:
        prefix_map = mapping.load_prefix_map()
    except Exception:
        pass
    prefix_re = re.compile(config.VC_PREFIX_RE)

    def resolve_cn(vc, default_title=""):
        if not vc:
            return default_title
        v_upper = vc.upper()
        if v_upper in vc_cn:
            return vc_cn[v_upper]
        m = prefix_re.match(v_upper)
        if m:
            pref = m.group(1).upper()
            if pref in prefix_map and prefix_map[pref].get("cn"):
                return prefix_map[pref]["cn"]
        return default_title

    return resolve_cn, vc_cn


def matches_filters(vc, cn, title, target_vcs=None, name_filter="", prefix_filter=""):
    """多条件综合过滤判断。"""
    vc_upper = (vc or "").upper()
    if target_vcs and vc_upper not in target_vcs:
        return False
    if prefix_filter and prefix_filter.upper() not in vc_upper:
        return False
    if name_filter:
        nl = name_filter.lower()
        if nl not in (cn or "").lower() and nl not in (title or "").lower():
            return False
    return True


def fetch_goods_from_wb(session, threshold, resolve_cn, target_vcs=None,
                        name_filter="", prefix_filter="", limit=0,
                        skip_target=None, page_size=PAGE_SIZE_DEF):
    """从 WB 原生接口按折扣降序拉取并筛选商品。"""
    items = []
    offset = 0
    pages = 0

    while pages < MAX_PAGES:
        body = {
            "limit": page_size,
            "offset": offset,
            "facets": [],
            "filterWithoutPrice": False,
            "filterWithLeftovers": False,
            "filterWithoutCompetitivePrice": False,
            "sort": "discount",
            "sortOrder": 0,
        }
        res = wb_api.request(session, "POST", DISC_LIST, json=body)
        goods = (res.get("data") or {}).get("listGoods") or []
        pages += 1
        if not goods:
            break

        first_d = common.to_int(goods[0].get("discount"))
        if threshold >= 0 and first_d is not None and first_d <= threshold:
            # 降序排列下首条 <= 阈值，后续无更高折扣，提前停止
            break

        for g in goods:
            disc = common.to_int(g.get("discount"))
            if threshold >= 0 and (disc is None or disc <= threshold):
                continue
            if skip_target is not None and disc == skip_target:
                continue

            vc = g.get("vendorCode") or ""
            title = g.get("title") or ""
            cn = resolve_cn(vc, title)

            if not matches_filters(vc, cn, title, target_vcs, name_filter, prefix_filter):
                continue

            prices = g.get("prices") or []
            price_val = prices[0] if prices else None

            items.append({
                "nmID": g.get("nmID"),
                "vendorCode": vc,
                "cn": cn,
                "old_discount": disc,
                "currencyIsoCode": g.get("isoCode4217") or "CNY",
                "price": price_val,
                "title": title,
            })
            if limit and len(items) >= limit:
                break

        if limit and len(items) >= limit:
            break
        if len(goods) < page_size:
            break
        offset += page_size
        time.sleep(0.2)

    return items


def collect_shop_target_items(sid, target_vcs, resolve_cn, limit=0, skip_target=None):
    """直接从本地快照定位目标 VC 的在架条目（用于单VC或全量品类直接改）。"""
    shop_rows = ops.load_shop_rows(sid)
    if not shop_rows:
        return None  # 无快照，回退到 WB 查询

    items = []
    for vc in target_vcs:
        r = shop_rows.get(vc)
        if not r:
            continue
        nm_id = r.get("nmId")
        if not nm_id:
            continue
        disc = common.to_int(r.get("discount"))
        if skip_target is not None and disc == skip_target:
            continue

        sl = r.get("sizeList") or []
        price_val = sl[0].get("price") if sl else None
        title = r.get("title") or ""
        cn = resolve_cn(vc, title)

        items.append({
            "nmID": nm_id,
            "vendorCode": vc,
            "cn": cn,
            "old_discount": disc,
            "currencyIsoCode": "CNY",
            "price": price_val,
            "title": title,
        })
        if limit and len(items) >= limit:
            break

    return items


def upload_batch_discounts(session, items, target_discount, chunk_size=CHUNK_DEF):
    """调用 WB 原生 upload/task 批量修改折扣。"""
    chunk_size = max(1, min(chunk_size, 300))
    results = {}
    total = len(items)

    for i in range(0, total, chunk_size):
        chunk = items[i:i + chunk_size]
        batch_idx = i // chunk_size + 1
        batch_count = (total + chunk_size - 1) // chunk_size

        payload_data = [
            {
                "vendorCode": it["vendorCode"],
                "nmID": it["nmID"],
                "discount": target_discount,
                "currencyIsoCode": it.get("currencyIsoCode") or "CNY",
            }
            for it in chunk
        ]
        body = {"data": payload_data}

        try:
            res = wb_api.request(session, "POST", DISC_UPLOAD, json=body)
            if not res.get("error"):
                task_id = (res.get("data") or {}).get("id")
                status_msg = f"成功(taskId={task_id})" if task_id else "成功"
                for it in chunk:
                    results[it["nmID"]] = status_msg
                print(f"    [批次 {batch_idx}/{batch_count}] 提交 {len(chunk)} 条 -> {status_msg}")
            else:
                err = res.get("errorText") or "未知错误"
                for it in chunk:
                    results[it["nmID"]] = f"失败({err})"
                print(f"    [批次 {batch_idx}/{batch_count}] 提交 {len(chunk)} 条 -> 失败: {err}")
        except Exception as e:
            err_msg = str(e)[:80]
            for it in chunk:
                results[it["nmID"]] = f"错误:{err_msg}"
            print(f"    [批次 {batch_idx}/{batch_count}] 提交 {len(chunk)} 条 -> 异常: {err_msg}")

        if i + chunk_size < total:
            time.sleep(CHUNK_SLEEP)

    return results


def verify_high_discounts(session, threshold, resolve_cn, target_vcs=None,
                          name_filter="", prefix_filter=""):
    """复核仍 > threshold 的商品列表。"""
    remain = fetch_goods_from_wb(
        session, threshold, resolve_cn,
        target_vcs=target_vcs, name_filter=name_filter, prefix_filter=prefix_filter
    )
    return [it["nmID"] for it in remain]


def write_csv(rows):
    """输出执行明细 CSV。"""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(config.LOG_DIR, f"折扣修改_wb_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "店铺",
                "shopId",
                "nmID",
                "vendorCode",
                "中文名",
                "标题",
                "原折扣",
                "目标折扣",
                "币种",
                "价格",
                "结果",
            ],
        )
        w.writeheader()
        w.writerows(rows)
    return path


def run(args):
    """统一 CLI 入口。"""
    common.ensure_utf8_stdout()
    cred = credentials.get()
    shops = cred.wb_shops()
    if not shops:
        print("[错误] credentials.json 中没有已填 cookie 的店铺")
        return 1

    # 1. 店铺筛选
    if getattr(args, "shops", None):
        want = {int(x) for x in str(args.shops).split(",") if x.strip()}
        shops = [s for s in shops if s["shopId"] in want]
        if not shops:
            print(f"[错误] 指定店铺 {args.shops} 未在凭证中找到")
            return 1

    # 2. 筛选条件解析
    raw_vc = getattr(args, "vc", "") or ""
    target_vcs = {v.strip().upper() for v in raw_vc.split(",") if v.strip()} if raw_vc else None
    name_filter = getattr(args, "name", "") or ""
    prefix_filter = getattr(args, "prefix", "") or ""
    is_all = getattr(args, "all", False)
    target = getattr(args, "target", config.DISCOUNT_TARGET_DEF)
    limit = getattr(args, "limit", 0)
    chunk_size = getattr(args, "chunk", CHUNK_DEF)
    do_verify = getattr(args, "verify", False)
    apply_mode = getattr(args, "apply", False)

    # 阈值判定：若显式传 --all 则不限阈值；若传 --vc 且未传 --threshold 也默认不限阈值；否则使用给定或默认 50
    raw_threshold = getattr(args, "threshold", None)
    if is_all:
        threshold = -1
    elif raw_threshold is not None:
        threshold = raw_threshold
    elif target_vcs:
        threshold = -1  # 显式指定具体 VC 时，默认直接处理该 VC
    else:
        threshold = config.DISCOUNT_THRESHOLD_DEF

    # 3. 构建 VC 归属解析器
    resolve_cn, vc_cn_dict = build_vc_resolver()

    # 打印运行计划
    pair_names = [f"{s.get('shopName', s['shopId'])}({s['shopId']})" for s in shops]
    filters_desc = []
    if target_vcs:
        filters_desc.append(f"VC={','.join(target_vcs)}")
    if name_filter:
        filters_desc.append(f"中文名包含'{name_filter}'")
    if prefix_filter:
        filters_desc.append(f"前缀={prefix_filter}")
    if threshold >= 0:
        filters_desc.append(f"原折扣 >{threshold}%")
    else:
        filters_desc.append("全部折扣")
    filters_str = " | ".join(filters_desc)

    print(f"店铺 {len(shops)} 个: {pair_names}")
    print(f"筛选条件: {filters_str} → 目标: {target}%{'' if apply_mode else '  [dry-run 预览]'}")
    print(f"模式: WB 原生批量（upload/task） | 默认写后验证: {'开启' if do_verify else '关闭（生效存在延迟）'}")

    all_rows = []
    total_found = 0
    total_success = 0
    total_failed = 0

    # 预先判断是否能走“已知集合直接定位”
    use_direct_target = False
    direct_vcs = []
    if threshold < 0:
        if target_vcs:
            use_direct_target = True
            direct_vcs = list(target_vcs)
        elif name_filter or prefix_filter:
            use_direct_target = True
            direct_vcs = [
                vc for vc, cn in vc_cn_dict.items()
                if matches_filters(vc, cn, "", None, name_filter, prefix_filter)
            ]

    for shop in shops:
        sname = shop.get("shopName", str(shop["shopId"]))
        sid = shop["shopId"]
        print(f"\n=== 店铺 {sname} ({sid}) ===")

        items = None
        # 若能走精准目标定位且本地快照存在，优先从本地提取（速度极快）
        if use_direct_target and direct_vcs:
            items = collect_shop_target_items(
                sid, direct_vcs, resolve_cn, limit=limit, skip_target=target
            )

        # 否则回退走 WB 原生列表实时扫描（高折扣过滤、快照缺失时）
        if items is None:
            try:
                session = wb_api.make_session(shop, cred.root_version)
                items = fetch_goods_from_wb(
                    session,
                    threshold,
                    resolve_cn,
                    target_vcs=target_vcs,
                    name_filter=name_filter,
                    prefix_filter=prefix_filter,
                    limit=limit,
                    skip_target=target,
                )
            except common.CookieExpiredError as e:
                print(f"  [警告] {e}（跳过本店）")
                continue
            except Exception as e:
                print(f"  [警告] 列表获取失败: {e}（跳过本店）")
                continue
        else:
            session = wb_api.make_session(shop, cred.root_version)

        if not items:
            print("  无匹配或符合条件的待改商品")
            continue

        total_found += len(items)
        print(f"  发现待改商品 {len(items)} 条")

        # 预览打印前 10 条
        preview_count = min(len(items), 10)
        for it in items[:preview_count]:
            old_d_str = f"{it['old_discount']}%" if it['old_discount'] is not None else "未知"
            cn_tag = f" [{it['cn']}]" if it['cn'] else ""
            print(f"    nmID={it['nmID']} vc={it['vendorCode']} "
                  f"{old_d_str} -> {target}%{cn_tag} | {it['title'][:35]}")
        if len(items) > preview_count:
            print(f"    ... 以及其余 {len(items) - preview_count} 条商品")

        shop_rows = []
        for it in items:
            shop_rows.append({
                "店铺": sname,
                "shopId": sid,
                "nmID": it["nmID"],
                "vendorCode": it["vendorCode"],
                "中文名": it["cn"],
                "标题": it["title"],
                "原折扣": it["old_discount"],
                "目标折扣": target,
                "币种": it.get("currencyIsoCode") or "CNY",
                "价格": it["price"],
                "结果": "DRY" if not apply_mode else "待提交",
            })

        if apply_mode:
            print(f"  [提交] 正在批量提交 {len(items)} 条（每批 ≤{chunk_size}）...")
            results = upload_batch_discounts(session, items, target, chunk_size=chunk_size)
            for row in shop_rows:
                res = results.get(row["nmID"], "未知")
                row["结果"] = res
                if "成功" in res:
                    total_success += 1
                else:
                    total_failed += 1

            if do_verify:
                print(f"  [回验] 等待 {VERIFY_WAIT} 秒后复核...")
                time.sleep(VERIFY_WAIT)
                remain = verify_high_discounts(
                    session, threshold, resolve_cn,
                    target_vcs=target_vcs, name_filter=name_filter, prefix_filter=prefix_filter
                )
                print(f"  [回验结果] 仍 >{threshold}% 剩余 {len(remain)} 个: {remain[:10]}")

        all_rows.extend(shop_rows)
        time.sleep(SHOP_SLEEP)

    csv_path = write_csv(all_rows) if all_rows else None
    print("\n" + "=" * 50)
    if apply_mode:
        print(f"[完成] 共匹配 {total_found} 条 | 成功 {total_success} 条 | 失败/异常 {total_failed} 条")
        if csv_path:
            print(f"[日志] 明细已保存至: {csv_path}")
        print("\n⚠ 重要提示：WB 平台降价/改折扣若使新价降幅落入 30-49.9%，商品将进入隔离区。")
        print("  请随后运行检查并审核价格：python wb.py price-review --apply")
    else:
        print(f"[dry-run 完成] 共匹配 {total_found} 条待改商品（未发起修改请求）")
        if csv_path:
            print(f"[预览日志] 清单已保存至: {csv_path}")
        print("（确认无误后加 --apply 参数执行真正修改）")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="WB 原生从高到低查询折扣并批量修改（upload/task 接口，支持单VC/多VC/中文名筛选，默认不做写后验证）"
    )
    parser.add_argument("--apply", action="store_true", help="真正提交修改（默认 dry-run 预览）")
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help=f"折扣阈值（处理 > 该值的商品；默认 {config.DISCOUNT_THRESHOLD_DEF}，指定 --all 或指定 --vc 时默认不限阈值）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="不限折扣阈值，处理指定条件下的所有在架商品（等价于 --threshold -1）",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=config.DISCOUNT_TARGET_DEF,
        help=f"目标折扣（默认 {config.DISCOUNT_TARGET_DEF}）",
    )
    parser.add_argument("--vc", default="", help="指定单个或多个 vendorCode（逗号分隔）")
    parser.add_argument("--name", default="", help="商品价格表产品中文名包含匹配（如 笔记本电脑）")
    parser.add_argument("--prefix", default="", help="vendorCode 4位前缀码包含匹配（如 DSGI）")
    parser.add_argument("--shops", default="", help="限定店铺 id 逗号分隔（默认凭证中所有店铺）")
    parser.add_argument("--limit", type=int, default=0, help="每店最多处理 N 条（0=不限）")
    parser.add_argument(
        "--chunk",
        type=int,
        default=CHUNK_DEF,
        help=f"每批提交条数（默认 {CHUNK_DEF}，最大 300）",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="执行后验证（默认关闭，因 WB 异步生效延迟）",
    )
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()

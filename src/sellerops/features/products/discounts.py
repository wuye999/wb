"""
sellerops 折扣管理业务服务 (Discounts)

编排 WB 原生降序拉取、本地品名反查、分片批量修改与执行日志沉淀。
"""
import sys
import os
import csv
import time
import datetime
from typing import List, Dict, Any

from sellerops.settings import paths as config
from sellerops.core import utils as common
from sellerops.settings import credentials
from sellerops.domain.models import DiscountPlan

from sellerops.persistence.snapshots import ProductSnapshotRepository
from sellerops.persistence.mappings import MappingRepository
from sellerops.integrations.wildberries.client import WBClient
from sellerops.integrations.wildberries import client as wb_api
from sellerops.features.products.skip_report import SkipTracker
from sellerops.features.products.discount_products import snapshot_discount_items


class Discounts:
    """折扣与价格调整用例服务"""

    def __init__(self):
        self.product_repo = ProductSnapshotRepository
        self.mapping_repo = MappingRepository

    def execute_plan(self, plan: DiscountPlan, shops: List[Dict[str, Any]], *, resolve_cn=None, shop_rows=None) -> Dict[str, Any]:
        """执行改折扣规划"""
        selected_by_name = resolve_cn is not None
        if resolve_cn is None:
            resolve_cn, _ = self.mapping_repo.build_vc_resolver()

        # 过滤目标店铺
        active_shops = shops
        if plan.target_shops:
            active_shops = [s for s in shops if (s.get("shopId") or s.get("shop_id")) in plan.target_shops]

        shop_names = [f"{s.get('shopName') or s.get('shop_name')}({s.get('shopId') or s.get('shop_id')})" for s in active_shops]
        print(f"店铺 {len(active_shops)} 个: {shop_names}")

        # 显示筛选提示
        conds = []
        if plan.name_filter:
            conds.append(f"中文名为'{plan.name_filter}'")
        if plan.target_vcs:
            conds.append(f"VC={','.join(plan.target_vcs[:3])}{'...' if len(plan.target_vcs) > 3 else ''}")
        if plan.prefix_filter:
            conds.append(f"前缀码='{plan.prefix_filter}'")
        if plan.is_all or (plan.threshold < 0 and plan.below < 0):
            conds.append("全部折扣")
        else:
            parts = []
            if plan.threshold >= 0:
                parts.append(f">{plan.threshold}%")
            if plan.below >= 0:
                parts.append(f"<{plan.below}%")
            conds.append("原折扣 " + " 或 ".join(parts))
        cond_str = " | ".join(conds) if conds else f"原折扣 >{plan.threshold}%"
        dry_str = "  [dry-run 预览]" if not plan.is_apply else ""
        print(f"筛选条件: {cond_str} → 目标: {plan.target_discount}%{dry_str}")
        if plan.below >= 0:
            print("  说明: 「<below」侧走 WB 折扣升序列表接口（list/goods/filter sortOrder=1），与降序侧并集去重")
        print("模式: WB 原生批量（upload/task） | 默认写后验证: 关闭（生效存在延迟）\n")

        all_rows: List[Dict[str, Any]] = []
        total_matched = 0
        total_applied = 0
        total_failed = 0
        quarantine_batches = 0
        price_modal_batches = 0
        task_ids: List[str] = []
        tracker = SkipTracker()

        for shop_dict in active_shops:
            sid = shop_dict.get("shopId") or shop_dict.get("shop_id")
            sname = shop_dict.get("shopName") or shop_dict.get("shop_name") or f"shop_{sid}"
            print(f"=== 店铺 {sname} ({sid}) ===")

            client = WBClient(shop_dict)
            matched_items: List[Dict[str, Any]] = []

            # 判断采集策略：若指定了 VC 或指定了 --all 且有本地快照，先走快照定位，否则走 WB 原生列表
            # （含 --below 的低折扣侧由 WB 折扣升序接口覆盖，见下方 fetch_discount_goods_asc）
            use_snapshot_direct = bool(plan.target_vcs or (plan.is_all and (plan.name_filter or plan.name_contains)))

            try:
                if use_snapshot_direct:
                    rows = shop_rows[sid] if shop_rows is not None else self.product_repo.load_shop_rows(sid)
                    if rows is None:
                        raise ValueError("目标店铺缺少快照")
                    matched_items, blocked = snapshot_discount_items(
                        sid, rows, plan, resolve_cn, self._matches_filters, self._disc_matched, tracker, from_name=selected_by_name)
                    total_failed += len(blocked)
                    all_rows.extend({'shop_id': sid, 'shop_name': sname, 'nmID': item.get('nmID'),
                                     'vendorCode': item['vc'], 'cn': item.get('cn', ''),
                                     'old_discount': None, 'new_discount': plan.target_discount,
                                     'status': 'skip', 'taskId': '', 'error': item['reason']} for item in blocked)
                else:
                    # 走 WB 原生列表：降序取「>threshold」侧；含 --below 时再取升序「<below」侧，并集去重
                    # ⚠ 只给 --below 时 threshold 为 -1（高折扣侧已关闭）→ 绝不能再拉降序侧：
                    #   降序接口靠「首条 ≤ threshold 截断」，threshold=-1 永不截断会翻遍全量目录（实测 12+ 分钟）
                    wb_goods: List[Dict[str, Any]] = []
                    if plan.is_all or plan.threshold >= 0:
                        wb_goods = client.fetch_discount_goods_desc(
                            threshold=-1 if plan.is_all else plan.threshold,
                            limit=plan.limit,
                        )
                    if plan.below >= 0 and not plan.is_all:
                        wb_goods = wb_goods + client.fetch_discount_goods_asc(
                            threshold=plan.below, limit=plan.limit,
                        )
                    seen_nm: set = set()
                    for g in wb_goods:
                        nm_key = g.get("nmID")
                        if nm_key in seen_nm:
                            continue
                        seen_nm.add(nm_key)
                        disc = common.to_int(g.get("discount"))
                        if not plan.is_all and (g.get("discount") is None or not self._disc_matched(disc, plan)):
                            continue
                        if disc == plan.target_discount:
                            continue
                        vc = g.get("vendorCode") or ""
                        title = g.get("title") or ""
                        cn = resolve_cn(vc, title)
                        if not self._matches_filters(vc, cn, title, plan):
                            continue
                        prices = g.get("prices") or []
                        price_val = prices[0] if prices else None
                        matched_items.append({
                            "nmID": g.get("nmID"),
                            "vendorCode": vc,
                            "cn": cn,
                            "old_discount": disc,
                            "currencyIsoCode": g.get("isoCode4217") or "CNY",
                            "price": price_val,
                            "title": title,
                        })
                        if plan.limit and len(matched_items) >= plan.limit:
                            break
            except Exception as error:
                reason = ('WB 网页会话失效，请运行 sellerops login wb' if isinstance(error, common.CookieExpiredError)
                          else f'本店商品准备失败（{type(error).__name__}）')
                targets = plan.target_vcs or ['（本店商品读取失败，无法枚举完整VC）']
                for vc in targets:
                    tracker.add(sid, vc, reason)
                    all_rows.append({'shop_id': sid, 'shop_name': sname, 'nmID': None,
                                     'vendorCode': vc, 'cn': '', 'old_discount': None,
                                     'new_discount': plan.target_discount, 'status': 'skip',
                                     'taskId': '', 'error': reason})
                total_failed += len(targets)
                print(f'  [已阻止] 店{sid}：{reason}')
                continue

            if not matched_items:
                print("  无匹配或符合条件的待改商品\n")
                continue

            print(f"  发现待改商品 {len(matched_items)} 条")
            total_matched += len(matched_items)

            for item in matched_items[:10]:
                cn_tag = f" [{item['cn']}]" if item.get("cn") else ""
                t_snip = (item.get("title") or "")[:30]
                print(f"    nmID={item['nmID']} vc={item['vendorCode']} {item['old_discount']}% -> {plan.target_discount}%{cn_tag} | {t_snip}")
            if len(matched_items) > 10:
                print(f"    ... 以及其余 {len(matched_items) - 10} 条商品")

            if not plan.is_apply:
                # dry-run 记录
                for item in matched_items:
                    all_rows.append({
                        "shop_id": sid,
                        "shop_name": sname,
                        "nmID": item["nmID"],
                        "vendorCode": item["vendorCode"],
                        "cn": item.get("cn") or "",
                        "old_discount": item["old_discount"],
                        "new_discount": plan.target_discount,
                        "status": "dry-run",
                        "taskId": "",
                        "error": "",
                    })
                print()
                continue

            # 真正提交修改
            print(f"  [提交] 正在批量提交 {len(matched_items)} 条（每批 ≤{plan.chunk_size}）...")
            chunks = [matched_items[i:i + plan.chunk_size] for i in range(0, len(matched_items), plan.chunk_size)]
            shop_ok = 0
            shop_fail = 0

            for i, chunk in enumerate(chunks, 1):
                payload = [
                    {
                        "vendorCode": str(x["vendorCode"]),
                        "nmID": int(x["nmID"]),
                        "discount": plan.target_discount,
                        "currencyIsoCode": x.get("currencyIsoCode") or "CNY",
                    }
                    for x in chunk
                ]
                try:
                    task_res = client.upload_batch_discount(payload)
                except common.CookieExpiredError as error:
                    for pending in matched_items[(i - 1) * plan.chunk_size:]:
                        all_rows.append({"shop_id": sid, "shop_name": sname, "nmID": pending["nmID"],
                                         "vendorCode": pending["vendorCode"], "cn": pending.get("cn", ""),
                                         "old_discount": pending["old_discount"], "new_discount": plan.target_discount,
                                         "status": "blocked", "taskId": "", "error": str(error)})
                        shop_fail += 1
                    break
                if task_res.success:
                    shop_ok += len(chunk)
                    modal_tag = self._modal_tag(task_res)
                    if getattr(task_res, "quarantine_modal", False):
                        quarantine_batches += 1
                    if getattr(task_res, "price_modal", False):
                        price_modal_batches += 1
                    if task_res.task_id:
                        task_ids.append(str(task_res.task_id))
                    print(f"    [批次 {i}/{len(chunks)}] 提交 {len(chunk)} 条 -> 成功"
                          f"(taskId={task_res.task_id}{modal_tag})")
                    for x in chunk:
                        all_rows.append({
                            "shop_id": sid,
                            "shop_name": sname,
                            "nmID": x["nmID"],
                            "vendorCode": x["vendorCode"],
                            "cn": x.get("cn") or "",
                            "old_discount": x["old_discount"],
                            "new_discount": plan.target_discount,
                            "status": "success",
                            "taskId": str(task_res.task_id or ""),
                            "error": "",
                        })
                else:
                    shop_fail += len(chunk)
                    print(f"    [批次 {i}/{len(chunks)}] 提交 {len(chunk)} 条 -> 失败: {task_res.error_message}")
                    for x in chunk:
                        all_rows.append({
                            "shop_id": sid,
                            "shop_name": sname,
                            "nmID": x["nmID"],
                            "vendorCode": x["vendorCode"],
                            "cn": x.get("cn") or "",
                            "old_discount": x["old_discount"],
                            "new_discount": plan.target_discount,
                            "status": "unknown" if "未知" in (task_res.error_message or "") else "fail",
                            "taskId": "",
                            "error": task_res.error_message,
                        })
                if i < len(chunks):
                    time.sleep(0.3)

            total_applied += shop_ok
            total_failed += shop_fail
            print()

        # 写入日志
        tracker.report('改折扣')
        log_path = self._write_csv(all_rows)

        print("=" * 50)
        if not plan.is_apply:
            print(f"[dry-run 完成] 共匹配 {total_matched} 条待改商品（未发起修改请求）")
            if log_path:
                print(f"[预览日志] 清单已保存至: {log_path}")
            print("（确认无误后加 --apply 参数执行真正修改）\n")
        else:
            unknown = sum(row["status"] == "unknown" for row in all_rows)
            print(f"[完成] 共匹配 {total_matched} 条 | 已受理 {total_applied} 条 | 失败或阻止 {total_failed - unknown} 条 | 结果未知 {unknown} 条")
            if task_ids:
                show = ",".join(task_ids[:5]) + ("..." if len(task_ids) > 5 else "")
                print(f"[任务号] 已落库 {len(task_ids)} 个提交任务: {show}")
            if price_modal_batches or quarantine_batches:
                print(f"[预检] 降价提示 {price_modal_batches} 批 · 隔离区提示 {quarantine_batches} 批"
                      f"（已自动确认并完成提交，无需人工点确认）")
            if log_path:
                print(f"[日志] 明细已保存至: {log_path}\n")

        return {
            "total_matched": total_matched,
            "total_applied": total_applied,
            "total_failed": total_failed,
            "log_path": log_path,
        }


    def apply_new_prices_by_nmids(self, shop: Dict[str, Any], nm_ids: List[Any]) -> Dict[str, Any]:
        """按 nmID 精确「应用新价格」（隔离区审核）。

        改价降价 30-49.9% 的商品会进 WB 价格审查（隔离区），**只有「应用新价格」才真正生效**。
        本方法只审核 nm_ids 命中的待审项，不触碰历史遗留待审商品。

        这是 replicate 域「改价后自动审核」（`price --auto-review`）的跨域入口 —— 按
        REUSE_GUIDE 铁律 3，跨域只能经本门面调用，禁止直接 import discount 域的私有实现。

        Args:
            shop: WB 店铺凭证 dict（含 shopId/shopName/cookie/authorizev3/wb_seller_lk）。
            nm_ids: 本次改价中降价落 30-49.9% 的 WB 商品码（nmID）列表。

        Returns:
            {"matched": 隔离区命中的数量, "applied": 成功应用的数量, "note": 人类可读说明}。
            调用方负责打印 note（服务层不打印，便于上层统一加 `[自动审核]` 前缀）。

        Raises:
            common.CookieExpiredError: 该店 cookie 失效（由上层捕获降级跳过）。
        """
        from sellerops.features.products import price_review as price_review
        wanted = [n for n in nm_ids if n is not None]
        if not wanted:
            return {"matched": 0, "applied": 0, "note": "无降价 30-49.9% 的商品，无需审核"}

        session = wb_api.make_session(shop)
        quarantine = price_review.fetch_all_quarantine(session)
        nm_to_id = {it.get("nmID"): it.get("id") for it in quarantine if it.get("nmID")}
        matched = [(n, nm_to_id[n]) for n in wanted if n in nm_to_id]
        if not matched:
            return {"matched": 0, "applied": 0,
                    "note": f"降价 30-49.9% 的 {len(wanted)} 个商品未在隔离区列表（可能尚未进入审查），跳过"}

        d = price_review.apply_prices(session, [qid for _, qid in matched])
        if d.get("error") is not False:
            return {"matched": len(matched), "applied": 0,
                    "note": f"应用新价格 {len(matched)} 个失败: {d.get('errorText') or 'error'}"}
        return {"matched": len(matched), "applied": len(matched),
                "note": f"应用新价格 {len(matched)} 个 nmID={[n for n, _ in matched]} ✓成功"}

    @staticmethod
    def _disc_matched(disc: int, plan: DiscountPlan) -> bool:
        """折扣区间判定（--threshold 与 --below 取并集）。

        Args:
            disc: 商品当前折扣（已转 int）。
            plan: 折扣规划（is_all / threshold / below）。

        Returns:
            是否命中待改条件：is_all=True 恒真；两侧阈值均未启用（如 --vc 精确定向，
            run_cli 会置 threshold=-1）同样视为「全部折扣」；否则「> threshold」或
            「< below」（各自 >=0 才启用）。
        """
        if plan.is_all:
            return True
        if plan.threshold < 0 and plan.below < 0:
            # 与 execute_plan 的条件展示（「全部折扣」）保持一致：
            # --vc 精确定向时 threshold=-1、below=-1，若不在此放行会导致
            # 任何折扣值都判为不匹配 → 命令恒输出「无匹配」（实测 2026-09-17）。
            return True
        if plan.threshold >= 0 and disc > plan.threshold:
            return True
        if plan.below >= 0 and disc < plan.below:
            return True
        return False

    @staticmethod
    def _matches_filters(vc: str, cn: str, title: str, plan: DiscountPlan) -> bool:
        v_upper = (vc or "").upper()
        if plan.target_vcs and v_upper not in {str(vc).upper() for vc in plan.target_vcs}:
            return False
        if plan.prefix_filter and plan.prefix_filter.upper() not in v_upper:
            return False
        if plan.name_filter:
            if plan.name_filter != cn:
                return False
        if plan.name_contains and plan.name_contains not in (cn or ""):
            return False
        return True

    @staticmethod
    def _modal_tag(task_res: Any) -> str:
        """把预检弹窗标记格式化为日志后缀（无弹窗返回空串）。"""
        tags = []
        if getattr(task_res, "price_modal", False):
            tags.append("降价提示")
        if getattr(task_res, "quarantine_modal", False):
            tags.append("隔离区提示")
        return f" · 预检: {'/'.join(tags)}" if tags else ""

    @staticmethod
    def _write_csv(rows: List[Dict[str, Any]]) -> str:
        if not rows:
            return ""
        os.makedirs(config.current.log_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(config.current.log_dir, f"折扣修改_wb_{ts}.csv")
        headers = ["shop_id", "shop_name", "nmID", "vendorCode", "cn", "old_discount", "new_discount", "status", "taskId", "error"]
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            w.writerows(rows)
        return out_path


def run_cli(args) -> int:
    """CLI 入口点，原 discount_wb.run 的直接替代实现"""
    cred = credentials.get()
    if not cred.shops:
        print("[错误] 未找到可用 WB 店铺凭证（请检查 credentials.json）", file=sys.stderr)
        return 1

    target_vcs = [x.strip() for x in (args.vc or "").split(",") if x.strip()] or None
    below = getattr(args, "below", -1)
    threshold = args.threshold if args.threshold is not None else -1
    is_all = threshold < 0 and below < 0
    target_shops = None
    if getattr(args, "shops", None):
        try:
            target_shops = [int(s.strip()) for s in args.shops.split(",") if s.strip()]
        except ValueError:
            print("[错误] --shops 参数格式不正确（示例: --shops <店铺ID>,<店铺ID>，可用 sellerops shop list 查看）", file=sys.stderr)
            return 1

    if target_shops:
        known = {int(shop["shopId"]) for shop in cred.shops if shop.get("shopId")}
        if missing := set(target_shops) - known:
            raise ValueError(f"目标店铺缺少 WB 配置：{sorted(missing)}")
    resolve_cn = shop_rows = None
    if any(getattr(args, field, None) for field in ("name", "name_contains", "sku", "prefix")):
        from sellerops.features.catalog.selection import load_state, resolve_filters
        selected = target_shops or [int(shop["shopId"]) for shop in cred.shops if shop.get("shopId")]
        state, shop_rows, book = load_state(selected)
        def resolve_cn(vc, title):
            return state.get(vc, {}).get("cn", "")
        target_vcs, description = resolve_filters(args, state, book)
        if not target_vcs:
            raise ValueError(f"{description} 没有已确认归属的链接")
        print(f"商品范围：{description}，{len(target_vcs)} 个完整 VC")
    plan = DiscountPlan(
        threshold=threshold,
        below=below,
        target_discount=getattr(args, "target", config.DISCOUNT_TARGET_DEF),
        name_filter="",
        prefix_filter="",
        target_vcs=target_vcs,
        target_shops=target_shops,
        limit=getattr(args, "limit", 0),
        chunk_size=getattr(args, "chunk", None) or 100,
        is_apply=getattr(args, "apply", False),
        is_all=is_all,
    )

    result = Discounts().execute_plan(plan, cred.shops, resolve_cn=resolve_cn, shop_rows=shop_rows)
    return 1 if result['total_failed'] else 0

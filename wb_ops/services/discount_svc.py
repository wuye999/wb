# -*- coding: utf-8 -*-
"""
wb_ops 折扣管理业务服务 (DiscountService)

编排 WB 原生降序拉取、本地品名反查、分片批量修改与执行日志沉淀。
"""
import sys
import os
import csv
import time
import datetime
from typing import List, Dict, Any, Optional

from .. import config, common, credentials
from ..domain.models import DiscountPlan, Product
from ..storage.product_repo import ProductSnapshotRepository
from ..storage.mapping_repo import MappingRepository
from ..adapters.wb_client import WBClient
from ..adapters import wb_client as wb_api


class DiscountService:
    """折扣与价格调整用例服务"""

    def __init__(self):
        self.product_repo = ProductSnapshotRepository
        self.mapping_repo = MappingRepository

    def execute_plan(self, plan: DiscountPlan, shops: List[Dict[str, Any]]) -> Dict[str, Any]:
        """执行改折扣规划"""
        resolve_cn, vc_cn = self.mapping_repo.build_vc_resolver()

        # 过滤目标店铺
        active_shops = shops
        if plan.target_shops:
            active_shops = [s for s in shops if (s.get("shopId") or s.get("shop_id")) in plan.target_shops]

        shop_names = [f"{s.get('shopName') or s.get('shop_name')}({s.get('shopId') or s.get('shop_id')})" for s in active_shops]
        print(f"店铺 {len(active_shops)} 个: {shop_names}")

        # 显示筛选提示
        conds = []
        if plan.name_filter:
            conds.append(f"中文名包含'{plan.name_filter}'")
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

        for shop_dict in active_shops:
            sid = shop_dict.get("shopId") or shop_dict.get("shop_id")
            sname = shop_dict.get("shopName") or shop_dict.get("shop_name") or f"shop_{sid}"
            print(f"=== 店铺 {sname} ({sid}) ===")

            client = WBClient(shop_dict)
            matched_items: List[Dict[str, Any]] = []

            # 判断采集策略：若指定了 VC 或指定了 --all 且有本地快照，先走快照定位，否则走 WB 原生列表
            # （含 --below 的低折扣侧由 WB 折扣升序接口覆盖，见下方 fetch_discount_goods_asc）
            use_snapshot_direct = bool(plan.target_vcs or (plan.is_all and plan.name_filter))
            
            if use_snapshot_direct and self.product_repo.exists(sid):
                shop_rows = self.product_repo.load_shop_rows(sid)
                search_pool = plan.target_vcs if plan.target_vcs else list(shop_rows.keys())
                for vc in search_pool:
                    r = shop_rows.get(vc)
                    if not r:
                        continue
                    nm_id = r.get("nmId") or r.get("nmID")
                    if not nm_id:
                        continue
                    title = r.get("shopGoodsName") or r.get("title") or ""
                    cn = resolve_cn(vc, title)
                    if not self._matches_filters(vc, cn, title, plan):
                        continue
                    disc = common.to_int(r.get("discount"))
                    if not plan.is_all and (r.get("discount") is None or not self._disc_matched(disc, plan)):
                        continue
                    if disc == plan.target_discount:
                        continue
                    matched_items.append({
                        "nmID": nm_id,
                        "vendorCode": vc,
                        "cn": cn,
                        "old_discount": disc,
                        "currencyIsoCode": r.get("currency") or "CNY",
                        "price": r.get("price") or r.get("salePrice"),
                        "title": title,
                    })
                    if plan.limit and len(matched_items) >= plan.limit:
                        break
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
                task_res = client.upload_batch_discount(payload)
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
                            "status": "fail",
                            "taskId": "",
                            "error": task_res.error_message,
                        })
                if i < len(chunks):
                    time.sleep(0.3)

            total_applied += shop_ok
            total_failed += shop_fail
            print()

        # 写入日志
        log_path = self._write_csv(all_rows)

        print("=" * 50)
        if not plan.is_apply:
            print(f"[dry-run 完成] 共匹配 {total_matched} 条待改商品（未发起修改请求）")
            if log_path:
                print(f"[预览日志] 清单已保存至: {log_path}")
            print("（确认无误后加 --apply 参数执行真正修改）\n")
        else:
            print(f"[完成] 共匹配 {total_matched} 条 | 成功 {total_applied} 条 | 失败/异常 {total_failed} 条")
            if task_ids:
                show = ",".join(task_ids[:5]) + ("..." if len(task_ids) > 5 else "")
                print(f"[任务号] 已落库 {len(task_ids)} 个提交任务: {show}")
            if price_modal_batches or quarantine_batches:
                print(f"[预检] 降价提示 {price_modal_batches} 批 · 隔离区提示 {quarantine_batches} 批"
                      f"（已自动确认并完成提交，无需人工点确认）")
            if log_path:
                print(f"[日志] 明细已保存至: {log_path}\n")
            print("[重要提示] WB 平台降价/改折扣若使新价降幅落入 30-49.9%，商品将进入隔离区。")
            print("  请随后运行检查并审核价格：python wb.py price-review --apply\n")

        return {
            "total_matched": total_matched,
            "total_applied": total_applied,
            "total_failed": total_failed,
            "log_path": log_path,
        }

    def run_bcs_discount(self, args: Any) -> int:
        """运行第二代 BCS 改折扣"""
        from .discount import discount_bcs
        return discount_bcs.run(args)

    def run_promo(self, args: Any) -> int:
        """运行促销活动报名"""
        from .discount import promo
        return promo.run(args)

    def run_price_review(self, args: Any) -> int:
        """运行价格审查"""
        from .discount import price_review
        return price_review.run(args)

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
        from .discount import price_review
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
        if d.get("error"):
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
        if plan.target_vcs and v_upper not in plan.target_vcs:
            return False
        if plan.prefix_filter and plan.prefix_filter.upper() not in v_upper:
            return False
        if plan.name_filter:
            nl = plan.name_filter.lower()
            if nl not in (cn or "").lower() and nl not in (title or "").lower():
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
        os.makedirs(config.LOG_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(config.LOG_DIR, f"折扣修改_wb_{ts}.csv")
        headers = ["shop_id", "shop_name", "nmID", "vendorCode", "cn", "old_discount", "new_discount", "status", "taskId", "error"]
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            w.writerows(rows)
        return out_path


discount_svc = DiscountService()


def run_cli(args) -> int:
    """CLI 入口点，原 discount_wb.run 的直接替代实现"""
    cred = credentials.get()
    if not cred.shops:
        print("[错误] 未找到可用 WB 店铺凭证（请检查 credentials.json）", file=sys.stderr)
        return 1

    target_vcs = [x.strip() for x in (args.vc or "").split(",") if x.strip()] or None
    is_all = getattr(args, "all", False)
    below = common.to_int(getattr(args, "below", -1), default=-1)

    if args.threshold is not None:
        threshold = args.threshold
    elif is_all or target_vcs or below >= 0:
        # --all / --vc：不限阈值；或只给了 --below → 关闭「>threshold」侧，仅按「<below」命中
        threshold = -1
    else:
        threshold = config.DISCOUNT_THRESHOLD_DEF

    target_shops = None
    if getattr(args, "shops", None):
        try:
            target_shops = [int(s.strip()) for s in args.shops.split(",") if s.strip()]
        except ValueError:
            print("[错误] --shops 参数格式不正确（示例: --shops <店铺ID>,<店铺ID>，可用 wb.py shops 查看）", file=sys.stderr)
            return 1

    plan = DiscountPlan(
        threshold=threshold,
        below=below,
        target_discount=getattr(args, "target", config.DISCOUNT_TARGET_DEF),
        name_filter=(getattr(args, "name", "") or "").strip(),
        prefix_filter=(getattr(args, "prefix", "") or "").strip(),
        target_vcs=target_vcs,
        target_shops=target_shops,
        limit=getattr(args, "limit", 0),
        chunk_size=getattr(args, "chunk", 100),
        is_apply=getattr(args, "apply", False),
        verify_after=getattr(args, "verify", False),
        is_all=is_all,
    )

    discount_svc.execute_plan(plan, cred.shops)
    return 0


def run_promo_apply(args):
    return discount_svc.run_promo(args)


def run_discount_bcs(args):
    return discount_svc.run_bcs_discount(args)


def run_price_review(args):
    return discount_svc.run_price_review(args)


"""价格、库存和折扣共用的逐店计数与结果输出。"""
from collections import Counter, defaultdict

from sellerops.core.command_summary import emit_summary
from sellerops.core.errors import AuthenticationError, StorageLockError
from sellerops.domain.snapshot_values import positive_id
from sellerops.persistence.product_results import ProductReceipt, append_product_history, write_product_receipts
from sellerops.settings import paths


class ProductResult:
    def __init__(self, action: str, channel: str, args) -> None:
        self.action = action
        self.channel = channel
        self.apply = getattr(args, "apply", False)
        self.shops = [int(sid) for sid in (getattr(args, "shops", "") or "").split(",") if sid]
        self.artifacts = {}
        self.plans = {}
        self.receipts: dict[int, list[ProductReceipt]] = defaultdict(list)
        self.errors: dict[int, str] = {}
        self.error = None
        self.cancelled = False
        self.review_requested = getattr(args, "auto_review", False)
        self.targets = {
            key: value for key, value in (
                ("price", getattr(args, "price", None)),
                ("discount", getattr(args, "discount", None) if action == "price" else
                 getattr(args, "target", None) if action == "discount" else None),
                ("clubDiscount", getattr(args, "club_discount", None)),
                ("amount", getattr(args, "amount", None)),
            ) if value is not None
        }
        if action == "price" and "price" not in self.targets and not getattr(args, "keep_price", False):
            self.targets["price"] = "price_book"
        self.submission_fields = list(self.targets)
        if channel == "bcs" and (action == "discount" or getattr(args, "keep_price", False)):
            self.submission_fields.insert(0, "price")

    def run(self, handler, args) -> int:
        try:
            handler(args, self)
        except Exception as error:
            self.error = f"{type(error).__name__}：{error}" if isinstance(error, (ValueError, OSError, AuthenticationError)) else type(error).__name__
            print(f"[错误] {self.error}")
        return self.finish()

    def add_plans(self, plans: list[dict]) -> None:
        for plan in plans:
            sid = plan["shopId"]
            self.plans[sid] = plan
            if sid not in self.shops:
                self.shops.append(sid)
            if report := plan.get("skip_report"):
                self.artifacts["skips_csv"] = report
            if error := plan.get("range_error"):
                self.errors[sid] = error
                if not plan.get("blocked_items"):
                    self.record(sid, {}, "blocked", error)
            for key, status in (("blocked_items", "blocked"), ("skipped_items", "skipped")):
                for item in plan.get(key, []):
                    self.record(sid, item, status, item["reason"])
            if not self.apply:
                for item in plan["items"]:
                    self.record(sid, item, "planned")

    @staticmethod
    def _card(item: dict) -> tuple[str, int | str] | None:
        if nm := positive_id(item.get("nmID")):
            return "nm", nm
        if vc := item.get("vc"):
            return "vc", vc
        return None

    def record(self, sid: int, item: dict, status: str, message: str = "",
               task_id: str | int | None = None, *, review: bool = False) -> None:
        action = self.action
        if review:
            action = "price_review"
        elif self.channel == "wb" or action == "discount":
            action = f"{action}_{self.channel}"
        target = {field: item.get(field, self.targets.get(field)) for field in self.submission_fields
                  if item.get(field, self.targets.get(field)) is not None}
        if self.action == "stock":
            target = target.get("amount")
        self.receipts[sid].append(ProductReceipt(sid, item, action, target, status, message,
                                                str(task_id) if task_id else None))

    def _shop(self, sid: int) -> tuple[dict, bool]:
        plan = self.plans.get(sid, {"items": []})
        items = plan["items"]
        is_stock = self.action == "stock"
        def key(item: dict) -> tuple:
            return item.get("vc"), item.get("chrtId")
        recorded = {key(receipt.item) for receipt in self.receipts[sid] if receipt.action != "price_review"}
        for item in items:
            if key(item) not in recorded:
                if self.apply and not self.cancelled:
                    self.record(sid, item, "blocked", self.error or "提交前中止")
                else:
                    self.record(sid, item, "planned")
        receipts = self.receipts[sid]
        writes = [r for r in receipts if r.action != "price_review"]
        review = [r for r in receipts if r.action == "price_review"]
        by_product = defaultdict(set)
        for r in writes:
            if card := self._card(r.item):
                by_product[card].add(r.status)
        statuses = ("blocked", "skipped", "accepted", "failed", "unknown")
        # 库存商品卡按最严重的规格回执分类；规格计数保留已受理部分。
        precedence = ("unknown", "failed", "blocked", "accepted", "skipped", "planned")
        product_counts = Counter(next(status for status in precedence if status in values)
                                 for values in by_product.values())
        matched = {card for r in writes if r.item.get("present", True) and (card := self._card(r.item))}
        range_error = self.errors.get(sid) or (self.error if sid not in self.plans else None)
        products = {status: product_counts[status] for status in statuses}
        products.update(matched=len(matched), submittable=len({self._card(it) for it in items}))
        if range_error:
            products.update(matched=None, blocked=None, skipped=None)
        row = {"shop_id": sid, "range_complete": range_error is None, "products": products,
               "task_ids": list(dict.fromkeys(r.task_id for r in writes if r.task_id))}
        if range_error:
            row["error"] = range_error
        prices = [it["price"] for it in items if it.get("price") is not None]
        if prices:
            low, high = min(prices), max(prices)
            row["planned_price"] = {"min": low, "max": high}
        if is_stock:
            spec_counts = Counter(r.status for r in writes if r.item.get("chrtId") is not None)
            specs = {status: spec_counts[status] for status in statuses}
            blocked_cards = [r.item for r in writes if r.status == "blocked" and "chrtId" not in r.item]
            blocked_ids = {spec for item in blocked_cards for spec in item.get("candidate_specs", [])}
            specs.update(matched=len(items) + len(blocked_ids), submittable=len(items))
            specs["blocked"] += len(blocked_ids)
            if range_error or any("candidate_specs" not in item for item in blocked_cards):
                specs.update(matched=None, blocked=None)
            if range_error:
                specs["skipped"] = None
            row["specs"] = specs
        if self.review_requested:
            counts = Counter(r.status for r in review)
            row["price_review"] = {status: counts[status] for status in statuses}
        for unit, counts in (("商品卡", products), ("规格", row.get("specs"))):
            if counts is None:
                continue
            labels = (("matched", "命中"), ("submittable", "可提交"), ("accepted", "已受理"),
                      ("blocked", "已阻止"), ("failed", "失败"), ("unknown", "结果未知"), ("skipped", "正常跳过"))
            figures = "，".join(f"{label} {counts[key] if counts[key] is not None else '未知'}"
                                for key, label in labels)
            print(f"店{sid}（{unit}）：{figures}")
        if review:
            counts = row["price_review"]
            print(f"店{sid}（价格审核）：已受理 {counts['accepted']}，已阻止 {counts['blocked']}，"
                  f"失败 {counts['failed']}，结果未知 {counts['unknown']}")
        bad = any(r.status in {"blocked", "failed", "unknown"} for r in receipts) or bool(range_error)
        return row, bad

    def finish(self) -> int:
        results = [self._shop(sid) for sid in self.shops]
        receipts = [receipt for sid in self.shops for receipt in self.receipts[sid]]
        history = self.apply and self.action in {"price", "stock"} and not self.cancelled
        try:
            path = write_product_receipts(paths.current, receipts)
            if path:
                self.artifacts["receipts_csv"] = path
            if history and (history_path := append_product_history(paths.current, receipts)):
                self.artifacts["history_csv"] = history_path
        except (OSError, StorageLockError) as error:
            self.error = f"无法保存回执：{error}"
            print(f"[错误] {self.error}")
        bad = bool(self.error or any(failed for _, failed in results))
        if bad:
            status = "partial" if any(r.status == "accepted" for r in receipts) else "failed"
        elif self.cancelled:
            status = "cancelled"
        elif not any(row["products"]["submittable"] for row, _ in results):
            status = "no_match"
        else:
            status = "completed" if self.apply else "preview"
        emit_summary(f"product {self.action}", "apply" if self.apply else "preview", status,
                     self.shops, self.artifacts, channel=self.channel, fields=list(self.targets),
                     targets=self.targets, submission_fields=self.submission_fields,
                     units={"products": "product_card", **({"specs": "specification"} if self.action == "stock" else {})},
                     per_shop=[row for row, _ in results],
                     **({"error": self.error} if self.error else {}))
        return 1 if bad else 0

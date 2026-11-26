"""只读运营报告实体；不包含凭证或平台会话。"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


@dataclass(frozen=True)
class ReportIssue:
    source: str
    message: str
    shop_id: int | None = None
    severity: str = '错误'


@dataclass
class ReportCatalog:
    price_names: set[str] = field(default_factory=set)
    mapped_names: dict[str, str] = field(default_factory=dict)
    historical_vcs: set[str] = field(default_factory=set)


@dataclass
class ProductReport:
    generated_at: str
    order_begin: str
    end: str
    feedback_begin: str = ''
    region: str = 'ru'
    shops: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    feedbacks: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[int, dict[str, Any]] = field(default_factory=dict)
    snapshots: dict[int, list[dict[str, Any]] | None] = field(default_factory=dict)
    publish_records: dict[str, Any] = field(default_factory=dict)
    publish_records_complete: bool = False
    issues: list[ReportIssue] = field(default_factory=list)
    # None 保持直接构建旧报告实体时的“所有店铺均为目标”行为。
    target_shop_ids: set[int] | None = None
    unavailable_accounts: list[str] = field(default_factory=list)
    catalog: ReportCatalog | None = None

    def recent_begin(self, days: int) -> str:
        return (date.fromisoformat(self.end) - timedelta(days=days - 1)).isoformat()

    @property
    def orders_collected_from(self) -> str:
        return min(self.order_begin, self.recent_begin(14))

    @property
    def orders_complete(self) -> bool:
        return not self.unavailable_accounts and all(
            self.coverage.get(shop['id'], {}).get('orders') == '完整' for shop in self.shops)

    @property
    def failed(self) -> bool:
        return any(issue.severity == '错误' for issue in self.issues)


@dataclass
class ProductScreening:
    low_rating: list[dict[str, Any]] = field(default_factory=list)
    high_returns: list[dict[str, Any]] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)
    issues: list[ReportIssue] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(issue.severity == '错误' for issue in self.issues)

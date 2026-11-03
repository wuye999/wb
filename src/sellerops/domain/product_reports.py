"""只读运营报告实体；不包含凭证或平台会话。"""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReportIssue:
    source: str
    message: str
    shop_id: int | None = None
    severity: str = '错误'


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
    sales: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[int, dict[str, Any]] = field(default_factory=dict)
    snapshots: dict[int, list[dict[str, Any]] | None] = field(default_factory=dict)
    publish_records: dict[str, Any] = field(default_factory=dict)
    publish_records_complete: bool = False
    issues: list[ReportIssue] = field(default_factory=list)
    feishu_complete: bool = False

    @property
    def failed(self) -> bool:
        return any(issue.severity == '错误' for issue in self.issues)

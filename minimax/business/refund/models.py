"""Domain models for the refund service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Order:
    """A paid order that may be refunded."""

    order_id: str
    paid_amount: Decimal
    paid_at: datetime
    shipped_at: datetime | None = None


@dataclass(frozen=True)
class RefundRequest:
    """One idempotent refund command."""

    request_id: str
    amount: Decimal
    reason: str = "customer_request"


@dataclass(frozen=True)
class RefundResult:
    """Outcome of one refund command."""

    request_id: str
    order_id: str
    status: str
    amount: Decimal
    reason_code: str | None = None
    detail: str = ""


@dataclass
class OrderLedger:
    """In-memory ledger used by the service and by tests."""

    refunds: dict[str, RefundResult] = field(default_factory=dict)
    by_order: dict[str, list[RefundResult]] = field(default_factory=dict)

    def succeeded_total(self, order_id: str) -> Decimal:
        total = Decimal("0.00")
        for result in self.by_order.get(order_id, []):
            if result.status == "succeeded":
                total += result.amount
        return total

    def record(self, result: RefundResult) -> None:
        self.refunds[result.request_id] = result
        self.by_order.setdefault(result.order_id, []).append(result)

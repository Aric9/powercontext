"""Refund service implementation.

The service is intentionally small: one command object in, one immutable result
out, with an in-memory ledger.  Behaviour follows ``business/specs/refund-policy.md``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from business.refund.models import Order, OrderLedger, RefundRequest, RefundResult
from business.refund.money import to_decimal

SERVICE_FEE_RATE = Decimal("0.90")
SERVICE_FEE_DAYS = 7


class RefundService:
    """Apply refund commands against orders and keep a ledger."""

    def __init__(self) -> None:
        self.ledger = OrderLedger()

    # ------------------------------------------------------------------ public
    def refund(
        self,
        order: Order,
        request: RefundRequest,
        now: datetime | None = None,
    ) -> RefundResult:
        """Refund ``request.amount`` for ``order``.

        Implements REF-001 (full refund), REF-002 (single-request cap),
        REF-004 (service fee), REF-005 (idempotency) and REF-007 (invalid input).
        """

        # REF-005: a repeated request id returns the first outcome unchanged.
        existing = self.ledger.refunds.get(request.request_id)
        if existing is not None:
            return existing

        amount = to_decimal(request.amount)

        if amount <= Decimal("0"):
            return self._reject(order, request, "invalid_amount", "refund amount must be positive")

        if amount > order.paid_amount:
            return self._reject(order, request, "exceeds_paid", "refund amount exceeds paid amount")

        if order.shipped_at is not None:
            age_days = (self._now(now) - order.shipped_at).days
            if age_days > SERVICE_FEE_DAYS:
                # REF-004: 10% service fee after the return window.
                amount = round(amount * SERVICE_FEE_RATE, 2)

        result = RefundResult(
            request_id=request.request_id,
            order_id=order.order_id,
            status="succeeded",
            amount=amount,
        )
        self.ledger.record(result)
        return result

    def refunded_total(self, order_id: str) -> Decimal:
        """Total successfully refunded amount for one order."""

        return self.ledger.succeeded_total(order_id)

    # ----------------------------------------------------------------- private
    def _reject(self, order: Order, request: RefundRequest, code: str, detail: str) -> RefundResult:
        result = RefundResult(
            request_id=request.request_id,
            order_id=order.order_id,
            status="rejected",
            amount=Decimal("0.00"),
            reason_code=code,
            detail=detail,
        )
        self.ledger.record(result)
        return result

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now

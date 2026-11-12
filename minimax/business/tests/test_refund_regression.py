"""Regression tests: behaviour that already works and must keep working."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from business.refund.models import Order, RefundRequest
from business.refund.service import RefundService

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def make_order(paid: str = "100.00", shipped_days_ago: int | None = None) -> Order:
    shipped_at = None if shipped_days_ago is None else NOW - timedelta(days=shipped_days_ago)
    return Order(
        order_id="ORD-REG",
        paid_amount=Decimal(paid),
        paid_at=NOW - timedelta(days=30),
        shipped_at=shipped_at,
    )


def test_full_refund_within_window() -> None:  # REF-001
    service = RefundService()
    result = service.refund(make_order("199.00"), RefundRequest("req-1", Decimal("199.00")), now=NOW)
    assert result.status == "succeeded"
    assert result.amount == Decimal("199.00")


def test_partial_refund_unshipped() -> None:
    service = RefundService()
    result = service.refund(make_order("199.00"), RefundRequest("req-2", Decimal("30.00")), now=NOW)
    assert result.status == "succeeded"
    assert result.amount == Decimal("30.00")
    assert service.refunded_total("ORD-REG") == Decimal("30.00")


def test_duplicate_request_is_idempotent() -> None:  # REF-005
    service = RefundService()
    order = make_order("100.00")
    first = service.refund(order, RefundRequest("req-dup", Decimal("40.00")), now=NOW)
    second = service.refund(order, RefundRequest("req-dup", Decimal("40.00")), now=NOW)
    assert first == second
    assert service.refunded_total("ORD-REG") == Decimal("40.00")


def test_negative_amount_is_rejected() -> None:  # REF-007
    service = RefundService()
    result = service.refund(make_order("100.00"), RefundRequest("req-neg", Decimal("-1.00")), now=NOW)
    assert result.status == "rejected"
    assert result.reason_code == "invalid_amount"


def test_single_request_above_paid_is_rejected() -> None:  # REF-002
    service = RefundService()
    result = service.refund(make_order("100.00"), RefundRequest("req-big", Decimal("100.01")), now=NOW)
    assert result.status == "rejected"
    assert result.reason_code == "exceeds_paid"

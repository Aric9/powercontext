"""Target tests for the current change request.

These tests describe the behaviour the task must add.  They fail on the initial
implementation and must pass after the change, without editing this file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from business.refund.models import Order, RefundRequest
from business.refund.service import RefundService

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def make_order(paid: str = "100.00", shipped_days_ago: int | None = None) -> Order:
    shipped_at = None if shipped_days_ago is None else NOW - timedelta(days=shipped_days_ago)
    return Order(
        order_id="ORD-TGT",
        paid_amount=Decimal(paid),
        paid_at=NOW - timedelta(days=30),
        shipped_at=shipped_at,
    )


def test_cumulative_refunds_cannot_exceed_paid() -> None:  # REF-003
    service = RefundService()
    order = make_order("100.00", shipped_days_ago=10)

    first = service.refund(order, RefundRequest("req-a", Decimal("60.00")), now=NOW)
    second = service.refund(order, RefundRequest("req-b", Decimal("60.00")), now=NOW)

    assert first.status == "succeeded"
    assert first.amount == Decimal("54.00")
    assert second.status == "rejected"
    assert second.reason_code == "exceeds_refundable"
    assert service.refunded_total("ORD-TGT") == Decimal("54.00")


def test_service_fee_rounds_half_up() -> None:  # REF-006
    service = RefundService()
    order = make_order("100.05", shipped_days_ago=10)

    result = service.refund(order, RefundRequest("req-round", Decimal("100.05")), now=NOW)

    assert result.status == "succeeded"
    assert result.amount == Decimal("90.05")


def test_rejection_does_not_consume_refundable_balance() -> None:  # REF-003
    service = RefundService()
    order = make_order("100.00", shipped_days_ago=10)

    assert service.refund(order, RefundRequest("req-x", Decimal("60.00")), now=NOW).amount == Decimal("54.00")
    rejected = service.refund(order, RefundRequest("req-y", Decimal("60.00")), now=NOW)
    assert rejected.status == "rejected"
    assert rejected.reason_code == "exceeds_refundable"

    final = service.refund(order, RefundRequest("req-z", Decimal("50.00")), now=NOW)
    assert final.status == "succeeded"
    assert final.amount == Decimal("45.00")
    assert service.refunded_total("ORD-TGT") == Decimal("99.00")

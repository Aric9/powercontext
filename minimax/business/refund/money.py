"""Decimal money helpers.

All monetary values in this service are :class:`~decimal.Decimal` instances with
two fractional digits.  Conversion helpers reject binary floats so that callers
cannot silently lose precision.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert an exact value to Decimal; floats are rejected on purpose."""

    if isinstance(value, float):
        raise TypeError("float is not accepted for money values; use Decimal or str")
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid money value: {value!r}") from exc


def round_money(value: Decimal) -> Decimal:
    """Round to cents using commercial half-up rounding (REF-006)."""

    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def format_money(value: Decimal) -> str:
    """Render money with exactly two fractional digits."""

    return f"{round_money(value):.2f}"

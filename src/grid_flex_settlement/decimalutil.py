"""Exact decimal arithmetic and deterministic cent allocation.

Settlement never uses binary floating point.  Quantities (kW, kWh) and money
carry :class:`~decimal.Decimal` values.  Rounding happens exactly once per
component, at the cent boundary, with banker's rounding.  The rounded component
total is then allocated to its contributing intervals with the largest-remainder
method, so the sum of the interval lines is *identical* to the component total.
Every allocation records its residual, making each cent traceable.
"""

from __future__ import annotations

from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
from typing import Any, Iterable, Sequence

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
MINOR = Decimal("100")


class AmountError(ValueError):
    """Raised for malformed decimal/money values."""


def dec(value: Any, *, field: str = "value", default: Decimal | None = None) -> Decimal:
    """Convert a JSON scalar to ``Decimal`` without passing through float noise."""

    if value is None:
        if default is not None:
            return default
        raise AmountError(f"{field}: missing number")
    if isinstance(value, str) and value.strip() == "":
        if default is not None:
            return default
        raise AmountError(f"{field}: empty number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise AmountError(f"{field}: boolean is not a number")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise AmountError(f"{field}: invalid decimal {value!r}") from exc
    else:
        raise AmountError(f"{field}: unsupported numeric type {type(value).__name__}")

    if not result.is_finite():
        raise AmountError(f"{field}: non-finite decimal")
    return result


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def to_minor(value: Decimal) -> int:
    """Convert an exact money value to integer cents using banker's rounding."""

    rounded = quantize_money(value)
    return int((rounded * MINOR).to_integral_value())


def from_minor(minor: int) -> Decimal:
    return Decimal(int(minor)).scaleb(-2)


def format_money(minor: int) -> str:
    """Format integer cents as a fixed two-decimal string (no float)."""

    sign = "-" if minor < 0 else ""
    amount = abs(int(minor))
    return f"{sign}{amount // 100}.{amount % 100:02d}"


def format_decimal(value: Decimal, *, places: int = 6) -> str:
    """Stable text form for quantities; trailing zeros are trimmed."""

    with localcontext() as ctx:
        ctx.prec = 40
        quantized = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def allocate_minor(
    total_minor: int,
    weights: Sequence[Decimal],
    keys: Sequence[str],
    *,
    fallback_weights: Sequence[Decimal] | None = None,
) -> list[int]:
    """Allocate ``total_minor`` cents proportionally with largest remainder.

    ``weights`` are exact (unrounded) line amounts.  Their signs normally match
    ``total_minor``; if they do not (for example when a positive total contains
    one adverse interval), absolute weights are used so the allocation remains
    stable and every cent is still assigned.  Ties break by ``keys`` sorted
    lexicographically, never by input order.
    """

    if len(weights) != len(keys):
        raise AmountError("weights and keys must have the same length")
    if not weights:
        if total_minor != 0:
            raise AmountError("cannot allocate a non-zero total to no lines")
        return []

    effective = [abs(item) for item in weights]
    if sum(effective) == 0 and fallback_weights is not None:
        effective = [abs(item) for item in fallback_weights]
    if sum(effective) == 0:
        effective = [ONE for _ in weights]

    sign = -1 if total_minor < 0 else 1
    magnitude = abs(int(total_minor))
    weight_total = sum(effective)
    with localcontext() as ctx:
        ctx.prec = 50
        exact = [Decimal(magnitude) * item / weight_total for item in effective]
    floors = [int(item.to_integral_value(rounding=ROUND_FLOOR)) for item in exact]
    remainder = magnitude - sum(floors)
    fractions = [item - Decimal(floor) for item, floor in zip(exact, floors)]
    order = sorted(range(len(weights)), key=lambda i: (-fractions[i], keys[i], i))
    for index in order[:remainder]:
        floors[index] += 1
    return [sign * item for item in floors]


def allocation_residuals(exact: Sequence[Decimal], allocated_minor: Sequence[int]) -> list[str]:
    return [
        format_decimal(item - from_minor(minor), places=12)
        for item, minor in zip(exact, allocated_minor)
    ]


def sum_decimals(values: Iterable[Decimal]) -> Decimal:
    total = ZERO
    for value in values:
        total += value
    return total

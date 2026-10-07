"""USD money: the decimal strings the API speaks, and the atomic integers the chain signs.

Money on the coordinator API is a decimal USD string. An atomic token integer
exists only where it is signed or verified — an order's ``rateIn``/``rateOut``,
a payment authorization's ``value`` — and it is ``usd × 10^decimals``, exactly,
with ``decimals`` the payment token's own, read from ``GET /evm/chain``.

A rate is USD per 1M units of work. The chain's ``RATE_SCALE`` is 10^6 units, so
the same shift gives the on-chain rate: ``"0.05"`` at 6 decimals is ``50000``.

Integer arithmetic end to end. A fraction longer than ``decimals`` is refused,
never rounded.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .errors import ValidationError, VorqError

#: ASCII digits only; no sign, exponent, whitespace, grouping or leading zeros;
#: a fraction needs digits on both sides of the point.
_USD = re.compile(r"(0|[1-9][0-9]*)(?:\.([0-9]+))?")

#: How a caller is told what a rate is.
RATE_HINT = 'USD per 1M units, e.g. "0.05"'


def parse_usd(text: str, decimals: int) -> int:
    """The atomic integer a USD string names at ``decimals``. Raises ``ValueError``."""
    if not isinstance(text, str):
        raise TypeError(f"{text!r} is not a USD string")
    match = _USD.fullmatch(text)
    if match is None:
        raise ValueError(f"{text!r} is not a USD decimal string")
    whole, fraction = match.group(1), match.group(2) or ""
    if len(fraction) > decimals:
        raise ValueError(f"{text!r} has more than {decimals} fraction digits")
    return int(whole) * 10**decimals + (int(fraction) * 10 ** (decimals - len(fraction)) if fraction else 0)


def format_usd(atomic: int, decimals: int) -> str:
    """The canonical USD string for an atomic integer: no trailing zeros, no bare point."""
    if isinstance(atomic, bool) or not isinstance(atomic, int) or atomic < 0:
        raise ValueError(f"{atomic!r} is not a non-negative atomic integer")
    whole, fraction = divmod(atomic, 10**decimals)
    if not fraction:
        return str(whole)
    return f"{whole}.{str(fraction).zfill(decimals).rstrip('0')}"


def usd_arg(value: object, field: str) -> str | None:
    """A caller's USD amount — ``str`` or ``Decimal``, nothing else — as its decimal string.

    ``None`` passes through. The grammar is checked here, before anything touches
    the network; the fraction's length is checked against the token's decimals
    when the amount is converted (:func:`usd_atomic`).
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, str):
        text = value
    else:
        raise ValidationError(
            f"{field} must be a str or Decimal in {RATE_HINT}, got "
            f"{type(value).__name__} {value!r}",
            type="invalid_request_error",
        )
    if _USD.fullmatch(text) is None:
        raise ValidationError(
            f"{field}={value!r} is not a USD decimal string ({RATE_HINT})",
            type="invalid_request_error",
        )
    return text


def usd_atomic(value: object, field: str, decimals: int) -> int | None:
    """A caller's USD amount as the atomic integer it is compared and signed in; ``None`` stays ``None``."""
    text = usd_arg(value, field)
    if text is None:
        return None
    try:
        return parse_usd(text, decimals)
    except ValueError as exc:
        raise ValidationError(
            f"{field}={text!r} has more fraction digits than the payment token's "
            f"{decimals} ({RATE_HINT})",
            type="invalid_request_error",
        ) from exc


def wire_usd(value: object, field: str, status_code: int | None = None) -> Decimal:
    """Money the node sent: a canonical USD string, or the node is at fault."""
    if isinstance(value, str):
        match = _USD.fullmatch(value)
        if match is not None and not (match.group(2) or "x").endswith("0"):
            return Decimal(value)
    raise VorqError(
        f"the node sent {field}={value!r}, which is not a canonical USD string",
        type="api_error", status_code=status_code,
    )


def wire_atomic(
    value: object, field: str, decimals: int, status_code: int | None = None
) -> int:
    """Money the node sent, as the atomic integer at ``decimals`` that is signed or checked."""
    wire_usd(value, field, status_code)
    try:
        return parse_usd(value, decimals)  # type: ignore[arg-type]
    except ValueError as exc:
        raise VorqError(
            f"the node sent {field}={value!r}, finer than the payment token's {decimals} decimals",
            type="api_error", status_code=status_code,
        ) from exc

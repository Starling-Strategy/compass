"""Single source for currency detection and canonical whole-dollar formatting.

Teacher-compensation metrics (salary/pay/wage/…) render as whole dollars
("$71,039") so a single response never mixes "$71,038.98" with "$71,038" — the
defect the `currency_format_canonical` criterion (#1687) flags. The ranking
value-formation path (`execution/ranking.py`) and the peer-comparison builder
(`execution/peer.py`) both use these helpers, so the currency tokens and the
parse/format rules live in exactly one place rather than drifting across copies.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Metric-name tokens that mark a column as money. Salary-shaped metrics are
# conventionally whole-dollar (cents are data-entry artifacts), so these are the
# columns we normalize.
CURRENCY_METRIC_TOKENS: tuple[str, ...] = (
    "salary",
    "salaries",
    "pay",
    "wage",
    "compensation",
    "stipend",
    "bonus",
)

# A bare currency amount: optional "$", digits with optional thousands commas and
# optional decimals. Used to pull a Decimal out of a stored display string.
_CURRENCY_VALUE_RE = re.compile(r"^\s*\$?\s*(-?\d[\d,]*(?:\.\d+)?)\s*$")


def haystack_has_currency_token(haystack: str) -> bool:
    """Whether any currency token appears in `haystack` (caller supplies the text)."""

    lowered = haystack.casefold()
    return any(token in lowered for token in CURRENCY_METRIC_TOKENS)


def metric_name_is_currency(metric_name: str | None) -> bool:
    """Whether a metric *name* alone marks the column as money."""

    return bool(metric_name) and haystack_has_currency_token(metric_name)


def currency_decimal(value: object) -> Decimal | None:
    """Parse a stored value (number or "$1,234.56" string) to a Decimal, or None."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    match = _CURRENCY_VALUE_RE.match(str(value))
    if match is None:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def whole_dollar_display(value: object) -> str | None:
    """Canonical whole-dollar string ("$71,039"), or None if `value` is not a
    parseable amount (caller keeps its existing display in that case)."""

    amount = currency_decimal(value)
    if amount is None:
        return None
    whole_dollars = int(amount.to_integral_value(rounding=ROUND_HALF_UP))
    return f"${whole_dollars:,}"

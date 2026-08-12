"""Tiny text-normalization helpers shared by execution-layer modules.

Pulled out so `_helpers.py` and the salary-disclosure module can both import
`metric_phrase_key` without a circular dependency.
"""

from __future__ import annotations

import re

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Single ellipsis convention for truncated display text: the one-character
# Unicode horizontal ellipsis, so a caller's length budget loses exactly one
# character to it (not three, as the old ASCII "..." form did).
_ELLIPSIS = "…"


def truncate(text: str, max_len: int) -> str:
    """Trim ``text`` to ``max_len`` retained characters, appending a single ``…``.

    ``max_len`` is the budget for the kept characters; the one-character
    ellipsis is added beyond it, so the result is at most ``max_len + 1``
    characters. Trailing whitespace exposed by the cut is stripped before the
    ellipsis. Text already within budget passes through unchanged. The single
    home for the truncate/ellipsis idiom — callers must not re-implement the
    budget math; a caller that needs the ellipsis to fit *inside* a total-length
    budget of ``N`` passes ``max_len=N - 1``.
    """

    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + _ELLIPSIS

# Canonical pattern for Compass display numerics: optional dollar sign, optional
# leading minus, digits with optional comma grouping and decimal, optional
# trailing percent sign, optional trailing "+". Anchored — rejects garbage like
# "12.5%abc" instead of silently consuming the trailing junk. The trailing "+"
# is the only divergence the count path's parser carried ("100+" → 100.0, a
# capped/at-least display); accepting it here makes this the single superset that
# every numeric-value parser can delegate to. This is a strict superset of the
# former canonical parser — the ONE behavioral change is that a trailing-"+" value
# now parses to its number on the ranking/lookup/trend paths where it previously
# returned None. Verified harmless today: 0 of compass.navigator_answers carry a
# trailing-"+" value (checked 2026-06-24), so no current outcome changes; and if one
# appeared, "100+" → 100.0 ranks the row instead of silently dropping it.
_COMPASS_NUMERIC_RE = re.compile(r"^\s*\$?\s*(-?\d[\d,]*(?:\.\d+)?)\s*%?\s*\+?\s*$")


def metric_phrase_key(value: str) -> str:
    """Collapse a metric phrase to a casefolded, single-space-separated key."""

    return _NON_ALNUM_RE.sub(" ", value.casefold()).strip()


def parse_compass_numeric_value(value: object) -> float | None:
    """Parse a Compass display numeric (e.g. ``"$1,234"``, ``"12.5%"``) to ``float``.

    Returns ``None`` for ``None``, booleans, or any string that does not match
    the canonical anchored pattern. Notably this *rejects* malformed inputs
    like ``"12.5%abc"`` — a previous ``operations.py`` parser used
    ``.rstrip("%")`` and silently accepted them as ``12.5``, which created a
    validator-vs-executor disagreement on what counts as numeric.

    The single string->number authority. A metric row's value is parsed exactly
    ONCE, at the row boundary: ``db.rows.MetricAnswerRow`` runs this in a
    ``model_validator`` and stores the result in ``numeric_value``. Numeric
    consumers (the filter, count, ranking, lookup-sort, and trend paths) read
    that typed field instead of re-parsing ``value`` — so the four divergent
    re-parsers that produced the #1772 crash class can never reappear (#1772 /
    #1773 / #1774). Remaining direct callers parse a *threshold* or *predicate
    input*, not a row value:

    - ``execution.count._parse_count_numeric`` — the filter-side comparison
      threshold (its trailing-``+`` divergence is folded into the canonical
      regex, ``"100+"`` → ``100.0``).
    - ``execution.operations`` — the filter/anchor comparison value at
      resolution time (parsed once when the resolved filter is built).
    - ``execution.lookup._lookup_value_is_strict_positive`` — a strict-positive
      predicate that mixes numeric and affirmative-text rules.

    Earlier divergent copies it superseded:

    - ``execution.ranking._RANK_NUMERIC_RE`` / ``_parse_rank_numeric_value``
    - ``execution.lookup._LOOKUP_NUMERIC_RE`` / ``_parse_lookup_numeric_value``
    - ``quality._validation_helpers._VALUE_SORT_NUMERIC_RE``
    - ``execution.operations._parse_numeric`` (the permissive ``rstrip`` variant
      whose acceptance of garbage is the bug surface this canonicalization
      closes — see audit finding #14).
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _COMPASS_NUMERIC_RE.match(str(value))
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


# Boolean DATA-value vocabularies for yes/no metric-value classification. These
# match cell values pulled from ``compass.policy_answers`` (e.g. ``"offered"``,
# ``"not addressed"``), NOT user prose — a ``validator-data-filter`` role under
# the no-prose-dispatch doctrine (see ``tests/test_no_prose_dispatch.py``).
# Promoted from byte-identical copies in ``operations.py`` and ``ranking.py``
# (#1419 backend dedup, finding 5) so the two classifiers can never drift.
_BOOLEAN_TRUE_VALUES = {"yes", "y", "true", "1", "offered", "available"}
_BOOLEAN_FALSE_VALUES = {
    "no",
    "n",
    "false",
    "0",
    "none",
    "n/a",
    "na",
    "ina",
    "unavailable",
    "not available",
    "issue not addressed",
    "not addressed",
}

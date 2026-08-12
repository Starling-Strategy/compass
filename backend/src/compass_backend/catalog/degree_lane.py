"""Shared degree-lane (BA vs MA) metric classifier.

Three sites used to carry parallel token logic for "is this metric in the
bachelor's lane or the master's lane": the catalog resolver's
``_candidate_lane`` (``resolution.py``), the execution layer's
``is_bachelor_starting_salary_metric`` (``execution/_salary_disclosure.py``),
and — with the opposite intent of *stripping* degree tokens for free-text
search — the ``_METRIC_STOPWORDS`` set in ``db/catalog.py``. This module is
the single home for the classification logic the first two sites need.

STOPGAP: this is the consolidated home for the token-based classifier. The
long-term goal — replacing it with a first-class ``degree_lane`` column on
``compass.navigator_metrics`` (future PR 2D+) — is unchanged. When that
column exists, callers should read it directly and this module can be
deleted.

``db/catalog.py`` is intentionally NOT a caller: its stopword list strips
degree tokens from a free-text search query so that, e.g., "bachelor's
starting salary" still returns BA *and* MA candidates for the resolver to
disambiguate. That inverse intent makes it unsafe to share state with the
classifier here — see the cross-reference comment in ``db/catalog.py``.
"""

from __future__ import annotations

import re
from typing import Literal

_LANE_TOKENS_BA: frozenset[str] = frozenset({"bachelor", "bachelors", "ba"})
_LANE_TOKENS_MA: frozenset[str] = frozenset({"master", "masters", "ma"})

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def classify_metric_lane(name: str) -> Literal["ba", "ma"] | None:
    """Token-based degree-lane classifier — see audit #17.

    Returns ``'ba'`` or ``'ma'`` when the casefolded, alphanumeric-split
    tokens of ``name`` intersect the corresponding lane set; ``None``
    otherwise. Splitting on non-alphanumeric characters means
    ``"bachelor's-degree"`` produces the whole token ``bachelor`` (a match),
    while ``"bachelorette-themed"`` produces the whole token
    ``bachelorette`` (no match) — that is the stricter whole-token contract,
    not the legacy substring check.

    Intended as a STOPGAP until ``degree_lane`` is a first-class column on
    ``compass.navigator_metrics``; see the module docstring for the
    long-term plan.
    """

    tokens = {
        token
        for token in _NON_ALNUM_RE.sub(" ", name.casefold()).split()
        if token
    }
    if tokens & _LANE_TOKENS_BA:
        return "ba"
    if tokens & _LANE_TOKENS_MA:
        return "ma"
    return None


# Lane tokens plus the bare qualifiers that ride with them: "degree", and the
# possessive fragment "s" that splitting "bachelor's" / "master's" on the
# apostrophe leaves behind. Removing these recovers the lane-neutral phrase.
_DEGREE_QUALIFIER_TOKENS: frozenset[str] = (
    _LANE_TOKENS_BA | _LANE_TOKENS_MA | frozenset({"degree", "s"})
)


def strip_degree_tokens(name: str) -> str:
    """Return ``name`` with degree-lane + bare-qualifier tokens removed.

    ``"starting salary BA degree"`` → ``"starting salary"``;
    ``"starting salary master's degree"`` → ``"starting salary"`` (the apostrophe
    split yields ``master`` + ``s``, both dropped, so BA and MA strip symmetrically).
    Same whole-token, casefolded, non-alphanumeric-split contract as
    :func:`classify_metric_lane` — only exact lane/qualifier tokens are removed, so
    ``"bachelorette planning"`` is untouched.

    Used to recover a re-searchable, lane-neutral phrase when a qualifier-extended
    metric phrase misses both the recall-alias and lexical search layers; the caller
    re-searches this and carries the classified lane forward so the existing
    lane filter collapses the recovered [BA, MA] bundle to the correct lane.

    Deliberately uses this module's lane authority, **not** ``db/catalog.py``'s
    ``_METRIC_STOPWORDS`` — that set is missing ``master``/``masters`` (it strips
    ``ba``/``bachelor``/``degree`` only), so sharing it would leave the MA lane broken.
    """

    return " ".join(
        token
        for token in _NON_ALNUM_RE.sub(" ", name.casefold()).split()
        if token and token not in _DEGREE_QUALIFIER_TOKENS
    )

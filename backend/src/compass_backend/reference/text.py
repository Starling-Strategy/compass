"""Low-level text normalization shared across Compass layers.

Lives in ``reference`` — the dependency leaf (it imports nothing from
``compass_backend``) — so execution, catalog, planning, orchestration, and
quality can all delegate to one normalizer without an import cycle.
"""

from __future__ import annotations

import re

_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalize_whitespace_casefold(value: str) -> str:
    """Collapse internal whitespace runs to one space, casefold, and strip.

    The single home for the casefold + whitespace normalizer that several
    modules each re-implemented (count, catalog resolution, planner thinking,
    region aliases, turn context, validator district names). Casefold — not
    ``lower`` — is canonical, so non-ASCII (e.g. German ``ß`` → ``ss``) folds
    consistently across every comparison surface.
    """

    return _WHITESPACE_RUN_RE.sub(" ", value.casefold()).strip()

"""W1-06 (#844): year literals must live in planning/temporal.py only.

A year literal like ``"2024 - 2025"`` in production code becomes a
silent fork of "current academic year" semantics. Rolling forward a year
should be a one-line change in ``planning/temporal.py``. This test scans
``src/compass_backend/`` for year literals and fails if any appear
outside that single module (tests excluded — fixtures legitimately pin
specific years).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCAN_ROOT = ROOT / "src" / "compass_backend"
ALLOWED_FILE = SCAN_ROOT / "planning" / "temporal.py"

# Match canonical academic-year shapes the codebase uses, e.g.
# "2023 - 2024", "2024 - 2025", "2024-2025".
_YEAR_RE = re.compile(r"\b202[3-9]\s*-\s*202[4-9]\b")


def _python_files() -> list[Path]:
    paths: list[Path] = []
    for path in SCAN_ROOT.rglob("*.py"):
        # Tests legitimately pin specific years for fixture data; the
        # boundary the W1-06 rule protects is production code only.
        if "/tests/" in str(path):
            continue
        # The canonical module is the one place year literals are allowed.
        if path == ALLOWED_FILE:
            continue
        paths.append(path)
    return paths


def test_no_academic_year_literals_outside_temporal() -> None:
    findings: list[str] = []
    for path in _python_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in _YEAR_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            snippet = text.splitlines()[line - 1].strip()
            findings.append(f"  {path.relative_to(ROOT)}:{line}: {snippet}")
    if findings:
        pytest.fail(
            "Academic-year literals must live only in "
            "src/compass_backend/planning/temporal.py "
            "(W1-06 / #844). New literal hits:\n"
            + "\n".join(findings)
            + "\n\nRoute the value through default_current_academic_year() "
            "or pass it explicitly from a caller that resolves through "
            "planning/temporal.py."
        )


def test_default_current_academic_year_returns_canonical_shape() -> None:
    """The helper must return a value the rest of the codebase already
    treats as canonical (so importing it is a drop-in replacement for the
    string literal it replaces)."""

    from compass_backend.planning.temporal import default_current_academic_year

    value = default_current_academic_year()
    assert _YEAR_RE.fullmatch(value), (
        f"default_current_academic_year must return a canonical shape; got {value!r}"
    )

"""Stable dashboard dimension values and their reviewer-facing labels."""

from __future__ import annotations

from collections.abc import Iterable

FLAG_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("not-sure", "Not sure"),
    ("selection-accuracy", "Selection Accuracy"),
    ("data-fidelity", "Data Fidelity"),
    ("coverage-state-labeling", "Coverage-State Labeling"),
    ("filter-accuracy", "Filter Accuracy"),
    ("sort-accuracy", "Sort Accuracy"),
    ("citation-accuracy", "Citation Accuracy"),
    ("consistency", "Consistency"),
    ("voice-tone", "Voice and tone"),
    ("exports-visuals", "Exports and visuals"),
    ("dashboard-workflow", "Dashboard workflow"),
)

_LABELS = dict(FLAG_DIMENSIONS)


def flag_dimension_label(value: str | None) -> str:
    """Return a friendly label while preserving historical free-text values."""
    if not value:
        return "Not assigned"
    return _LABELS.get(value, value.replace("_", " ").replace("-", " ").capitalize())


def flag_dimension_options(existing: Iterable[str] = ()) -> tuple[tuple[str, str], ...]:
    """Known stable values plus any historical value already stored in a report."""
    known = set(_LABELS)
    legacy = sorted({value for value in existing if value and value not in known})
    return (*FLAG_DIMENSIONS, *((value, flag_dimension_label(value)) for value in legacy))

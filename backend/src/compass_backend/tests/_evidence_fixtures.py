"""Shared `VerdictEvidence` factory for test fixtures.

The underscore prefix prevents pytest from collecting this as a test module.

After the persistence-seam typing landed, `VerdictRecord.evidence` is a typed
`VerdictEvidence` model rather than a bare `dict`. Tests that construct
`VerdictRecord` directly need to supply the three required envelope keys.
"""

from __future__ import annotations

from typing import Any


def default_evidence_dict(**extras: Any) -> dict[str, Any]:
    """Return a minimal valid evidence envelope dict + any extra keys.

    Returned as a dict (not a typed model) so call sites can pass it directly
    to `VerdictRecord(evidence=...)` — Pydantic coerces the dict into a
    `VerdictEvidence` instance. `extra='allow'` on the model preserves
    arbitrary extra keys per the SSN-201 contract.
    """
    return {
        "artifact_snapshot": {},
        "answer_excerpt": "",
        "copied_span_attributes": {},
        **extras,
    }


__all__ = ["default_evidence_dict"]

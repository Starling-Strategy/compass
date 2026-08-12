"""Stable content-hash identity for deterministic Compass result artifacts.

Compass's surface-consistency invariant — "every rendered surface derives
from the same canonical `ResultSet`" — is enforced by stamping a short
content-hash on each surface as it's emitted, then asserting at validation
time that every surface carries the same id.

`compute_artifact_id` returns that stamp. It's a free function (not a model
field) for three reasons:

1. **No schema churn.** Hundreds of test fixtures construct `ResultSet`
   variants; adding a field would require a default + validator-populates
   pattern that Pydantic v2 docs warn against.
2. **No validator-ordering risk.** A second `mode="after"` validator
   running alongside `populate_artifact_surfaces` would be load-bearing
   across the six variants; one missed inheritance and the stamp is wrong.
3. **Compute at emit time.** The stamp is meaningful when it ships with a
   surface. The renderer writes it into `manifest.metadata`; the SSE
   serializer writes it into the data payload. Re-computed at validation
   time, it can be compared to the stamped values.

The hash is 16 hex chars (64 bits) — sufficient for per-turn-grain
collision-resistance without flooding logs.
"""

from __future__ import annotations

import hashlib
import json

from compass_backend.artifacts.results import ResultSet


def compute_artifact_id(result: ResultSet) -> str:
    """Return a stable 16-hex-char SHA256 prefix of the ResultSet's content.

    Uses `model_dump(mode="json")` to project the model into a JSON-shaped
    dict, then `json.dumps(..., sort_keys=True)` to produce a canonical
    byte sequence regardless of insertion order. Cross-process stable
    because (a) Pydantic's `mode="json"` is deterministic for our field
    types, and (b) `sort_keys=True` strips out dict-order variation.

    Includes every field — every value that affects a rendered surface
    should affect the id. If two ResultSets render the same output, their
    ids should match; if they don't, the ids should differ.
    """
    payload = json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = ["compute_artifact_id"]

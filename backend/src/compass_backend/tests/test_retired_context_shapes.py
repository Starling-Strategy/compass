"""Static guardrails for retired Compass conversation-context shapes."""

from __future__ import annotations

from pathlib import Path


RETIRED_TOKENS = (
    "case_context",
    "districts_of_interest",
    "topics_of_interest",
    "last_comparison",
    "last_metric_ids",
    "next_citation_id",
    "PlannerContextFrame",
)


def test_retired_context_shapes_do_not_reappear_in_active_runtime_code() -> None:
    root = Path(__file__).resolve().parents[3]
    runtime_roots = (root / "src" / "compass_backend", root / "src" / "nctqai")
    offenders: list[str] = []

    for runtime_root in runtime_roots:
        for path in runtime_root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for token in RETIRED_TOKENS:
                if token in text:
                    offenders.append(f"{path.relative_to(root)} contains {token}")

    assert offenders == []

"""Regression for #1757 — real-content policy-guidance citations resolve to the
real per-topic NCTQ research-rationale page, not the Pathfinder homepage.

The fix is data-only (``content/nctq-policy/topics/*.md``): six topics had their
~29 rationale ``source_url``s and their topic ``canonical_url`` pointing at the
bare Pathfinder homepage with ``citation_status: placeholder``. Those now point at
the verified ``.../research-rationales/<slug>/`` page with ``citation_status:
ready``.

These tests load the *real* library (not a synthetic fixture) so they fail if the
content drifts back to the homepage placeholder or a status/URL pair desyncs.
The renderer is unchanged — the symmetric validator + the ``canonical_url``-derived
stance status do the work — so a render of the real ``evaluation`` bundle (the
client's literal-prompt topic) is the behavioral proof of the exit criterion.
"""

from __future__ import annotations

import pytest

from compass_backend.contracts import PolicyGuidancePlan
from compass_backend.policy_guidance.loader import load_library
from compass_backend.rendering.policy_guidance import render_policy_guidance

_RR = "https://www.nctq.org/district-policy-pathfinder/research-rationales/"
_HOMEPAGE = "https://www.nctq.org/district-policy-pathfinder/"

# Verified against NCTQ's live WordPress REST API (parent page id 12686) and an
# independent curl with a 404 control. Note differentiated-pay → the merged
# "Differentiated and Performance Pay" page (slug ≠ filename).
_EXPECTED_TOPIC_URL = {
    "benefits": f"{_RR}benefits/",
    "differentiated-pay": f"{_RR}differentiated-compensation/",
    "evaluation": f"{_RR}evaluation/",
    "general-salary": f"{_RR}general-salary/",
    "leave": f"{_RR}leave/",
    "teacher-time": f"{_RR}teacher-time/",
}


@pytest.mark.parametrize(("topic_id", "expected_url"), _EXPECTED_TOPIC_URL.items())
def test_backfilled_topic_citations_are_ready_and_real(
    topic_id: str, expected_url: str
) -> None:
    """Every backfilled topic: canonical_url set to the real page, and every
    rationale is `ready` with that same real source_url — never the homepage."""
    topic = load_library().topics[topic_id]

    assert topic.canonical_url is not None, f"{topic_id}: canonical_url still empty"
    assert str(topic.canonical_url) == expected_url

    assert topic.rationales, f"{topic_id}: expected backfilled rationales"
    for rationale in topic.rationales:
        assert rationale.citation_status == "ready", rationale.rationale_id
        assert str(rationale.source_url) == expected_url, rationale.rationale_id
        assert str(rationale.source_url).rstrip("/") != _HOMEPAGE.rstrip("/")


def test_evaluation_render_has_no_provisional_marker() -> None:
    """Exit criterion: rendering the real `evaluation` stance+rationale bundle
    (the client's literal prompt) carries the real research-rationale URL and
    NO `[provisional source]` marker — the homepage stopgap is gone."""
    library = load_library()
    plan = PolicyGuidancePlan(
        topic_ids=["evaluation"],
        layers=["stances", "rationales"],
        intent_summary="What is NCTQ's position on teacher evaluations?",
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    assert "[provisional source]" not in manifest.body
    statuses = {c["citation_status"] for c in manifest.metadata["citations"]}
    assert statuses == {"ready"}
    urls = {str(c["url"]) for c in manifest.metadata["citations"]}
    assert urls == {f"{_RR}evaluation/"}

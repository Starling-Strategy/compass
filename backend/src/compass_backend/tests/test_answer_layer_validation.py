"""Validation guardrails for answer-layer drafts."""

from __future__ import annotations

import pytest

from compass_backend.answer_layer.briefs import build_answer_brief
from compass_backend.answer_layer.validation import validate_answer_draft
from compass_backend.contracts.answer_layer import AnswerBrief, AnswerDraft, ImmutableBlock
from compass_backend.contracts.rendering import ResponseManifest


DETERMINISTIC_BODY = """Denver's starting salary is $55,000 for 2024-2025. [1]

This uses currently reviewed data and may not include every schedule lane.

| District | State | Starting salary | Sources |
| --- | --- | ---: | --- |
| Denver Public Schools | CO | $55,000 | [1] |

#### Methodology
- Bachelor's-lane starting salary.

Sources

- [1] Denver collective bargaining agreement
"""


def _brief(*, allowed_nctq_context: tuple[str, ...] = ()):
    return build_answer_brief(
        ResponseManifest(
            body=DETERMINISTIC_BODY,
            status="rendered",
            result_type="metric_lookup",
            validation_valid=True,
            metadata={"question": "What is Denver's salary schedule?"},
        ),
        caveat_fragments=("currently reviewed data", "may not include every schedule lane"),
        allowed_nctq_context=allowed_nctq_context,
    )


VALID_DRAFT_BODY = """Compass can show Denver's reviewed starting-salary data for 2024-2025.

This uses currently reviewed data and may not include every schedule lane.

| District | State | Starting salary | Sources |
| --- | --- | ---: | --- |
| Denver Public Schools | CO | $55,000 | [1] |

#### Methodology
- Bachelor's-lane starting salary.

Sources

- [1] Denver collective bargaining agreement
"""


def test_validate_answer_draft_accepts_preserved_safe_revision() -> None:
    report = validate_answer_draft(_brief(), AnswerDraft(body=VALID_DRAFT_BODY), mode="gated")

    assert report.accepted is True
    assert report.findings == ()


@pytest.mark.parametrize(
    ("body", "finding"),
    [
        ("Compass found Denver salary data, but the table is omitted.", "missing_immutable_block"),
        # #1228: dropping a sealed '#### ' section heading fails the seal ->
        # the draft is rejected -> the answer-first deterministic body ships.
        (VALID_DRAFT_BODY.replace("#### Methodology", "Methodology"), "missing_immutable_block"),
        (VALID_DRAFT_BODY.replace("$55,000", "$56,000", 1), "new_numeric_token"),
        (VALID_DRAFT_BODY.replace("agreement", "agreement [2]"), "new_source_marker"),
        (
            VALID_DRAFT_BODY.replace("This uses currently reviewed data and ", ""),
            "missing_caveat_fragment",
        ),
        (VALID_DRAFT_BODY + "\n\nInternal note: ResultSet validator passed.", "banned_internal_term"),
        ("Too short.", "body_too_short"),
        (
            VALID_DRAFT_BODY
            + "\n\nNCTQ recommends using this district as a national model for pay policy.",
            "unsupported_nctq_context",
        ),
    ],
)
def test_validate_answer_draft_rejects_unsafe_revisions(body: str, finding: str) -> None:
    report = validate_answer_draft(_brief(), AnswerDraft(body=body), mode="gated")

    assert report.accepted is False
    assert any(item.code == finding for item in report.findings)


def test_validate_answer_draft_allows_admitted_nctq_context() -> None:
    allowed = "NCTQ has curated examples for teacher compensation policies."
    body = VALID_DRAFT_BODY + f"\n\n{allowed}"

    report = validate_answer_draft(
        _brief(allowed_nctq_context=(allowed,)),
        AnswerDraft(body=body),
        mode="gated",
    )

    assert report.accepted is True


def test_validate_answer_draft_rejects_removed_auto_coverage_caveat() -> None:
    body = """Compass found partial data.

Data availability: Of the 5 districts you asked about, 2 have current reviewed data; 3 haven't been reviewed for 2024 - 2025 yet.

| District | Value |
| --- | --- |
| Alpha | Yes |
"""
    brief = build_answer_brief(
        ResponseManifest(
            body=body,
            status="rendered",
            result_type="metric_lookup",
            validation_valid=True,
        )
    )
    draft = AnswerDraft(
        body="""Compass found partial data.

| District | Value |
| --- | --- |
| Alpha | Yes |
"""
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is False
    assert any(item.code == "missing_caveat_fragment" for item in report.findings)


def test_validate_answer_draft_rejects_dropped_stale_year_caveat() -> None:
    """#1356/#1514: the stale prior-year state is voiced ONLY as the canonical
    D7 sentence ("NCTQ last reviewed … ; the value then was …"). The gated
    layer must reject a styled draft that paraphrases the sentence away — the
    deterministic enforcement behind the stylist coverage-fidelity guide rule.
    """
    body = """Compass found partial data.

NCTQ last reviewed Broward County for starting salary in 2022 - 2023; the value then was $48,000.

| District | Value |
| --- | --- |
| Alpha | $52,000 |
"""
    brief = build_answer_brief(
        ResponseManifest(
            body=body,
            status="rendered",
            result_type="metric_lookup",
            validation_valid=True,
        )
    )
    draft = AnswerDraft(
        body="""Compass found partial data.

Broward County has data on file, but only from an older year.

| District | Value |
| --- | --- |
| Alpha | $52,000 |
"""
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is False
    assert any(item.code == "missing_caveat_fragment" for item in report.findings)


def test_validation_accepts_nctq_phrasing_when_url_anchor_present() -> None:
    brief = AnswerBrief(
        deterministic_body=(
            "Compass found one Denver row.\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |\n\n"
            "Sources:\n[1] Denver contract"
        ),
        user_question="What is Denver's starting salary?",
        route="execute",
        result_type="metric_lookup",
        immutable_blocks=(
            ImmutableBlock(
                kind="table",
                text="| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |",
                block_id="table-1",
            ),
            ImmutableBlock(
                kind="sources",
                text="Sources:\n[1] Denver contract",
                block_id="sources-1",
            ),
        ),
        numeric_tokens=("$55,000",),
        source_markers=("[1]",),
        allowed_nctq_context=(
            "[rationale] Competitive Pay — Districts should pay competitively.\n"
            "URL: https://www.nctq.org/district-policy-pathfinder/",
        ),
    )
    draft = AnswerDraft(
        body=(
            "Denver's starting salary is $55,000. NCTQ's policy stance on this "
            "is that districts should pay teachers competitively "
            "(https://www.nctq.org/district-policy-pathfinder/).\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |\n\n"
            "Sources:\n[1] Denver contract"
        )
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is True


def test_validation_rejects_fabricated_absence_claim() -> None:
    """#1070: the stylist invents "Compass doesn't have reviewed data for X"
    about an entity that is merely missing from this turn's result rows (the
    value was reviewed and shown a prior turn, still in memory.prior_results).
    The deterministic body never makes that absence claim, so the gated layer
    must reject the draft and fall back to the deterministic answer.
    """
    deterministic = (
        "Here are the two reviewed Vermont districts for first-year BA salary.\n\n"
        "| District | State | First-year BA salary | Sources |\n"
        "| --- | --- | ---: | --- |\n"
        "| Burlington School District | VT | $44,000 | [1] |\n"
        "| Montpelier Roxbury | VT | $43,500 | [1] |\n\n"
        "Sources\n\n- [1] Vermont collective bargaining agreements"
    )
    brief = build_answer_brief(
        ResponseManifest(
            body=deterministic,
            status="rendered",
            result_type="metric_lookup",
            validation_valid=True,
            metadata={"question": "How does Dallas ISD compare to Vermont?"},
        )
    )
    draft = AnswerDraft(
        body=(
            "Compass doesn't currently have reviewed data for Dallas ISD's "
            "first-year BA salary, so a direct comparison isn't possible right now. "
            "Here are the two reviewed Vermont districts.\n\n"
            "| District | State | First-year BA salary | Sources |\n"
            "| --- | --- | ---: | --- |\n"
            "| Burlington School District | VT | $44,000 | [1] |\n"
            "| Montpelier Roxbury | VT | $43,500 | [1] |\n\n"
            "Sources\n\n- [1] Vermont collective bargaining agreements"
        )
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is False
    assert any(
        finding.code == "fabricated_absence_claim" for finding in report.findings
    )


def test_validation_preserves_renderer_written_absence_prose() -> None:
    """The mirror case: when the renderer itself states genuine absence in the
    deterministic body, the stylist preserving that prose is allowed — the
    fabrication check fires only on absence claims the stylist ADDED.
    """
    deterministic = (
        "Compass does not have reviewed data for first-year BA salary in "
        "Dallas ISD.\n\n"
        "| District | State | First-year BA salary | Sources |\n"
        "| --- | --- | ---: | --- |\n"
        "| Burlington School District | VT | $44,000 | [1] |\n\n"
        "Sources\n\n- [1] Vermont collective bargaining agreements"
    )
    brief = build_answer_brief(
        ResponseManifest(
            body=deterministic,
            status="rendered",
            result_type="metric_lookup",
            validation_valid=True,
            metadata={"question": "Dallas ISD first-year BA salary?"},
        )
    )
    draft = AnswerDraft(body=deterministic)

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is True


def test_validation_rejects_nctq_phrasing_without_url_anchor() -> None:
    brief = AnswerBrief(
        deterministic_body=(
            "Denver starting salary table.\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
        ),
        user_question="x",
        route="execute",
        result_type="metric_lookup",
        immutable_blocks=(
            ImmutableBlock(
                kind="table",
                text="| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |",
                block_id="table-1",
            ),
        ),
        numeric_tokens=("$55,000",),
        allowed_nctq_context=(
            "[rationale] Competitive Pay — Districts should pay competitively.\n"
            "URL: https://www.nctq.org/district-policy-pathfinder/",
        ),
    )
    draft = AnswerDraft(
        body=(
            "Denver's starting salary is $55,000. NCTQ recommends paying "
            "teachers more.\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
        )
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is False
    assert any(
        finding.code == "nctq_url_anchor_missing"
        for finding in report.findings
    )


def test_validation_skips_anchor_check_when_no_nctq_phrasing() -> None:
    brief = AnswerBrief(
        deterministic_body=(
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
        ),
        user_question="x",
        route="execute",
        result_type="metric_lookup",
        immutable_blocks=(
            ImmutableBlock(
                kind="table",
                text="| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |",
                block_id="table-1",
            ),
        ),
        numeric_tokens=("$55,000",),
        allowed_nctq_context=(
            "[rationale] Competitive Pay — Districts should pay competitively.\n"
            "URL: https://www.nctq.org/district-policy-pathfinder/",
        ),
    )
    draft = AnswerDraft(
        body=(
            "Denver's starting salary is $55,000.\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
        )
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is True


def test_validation_accepts_preserved_policy_stance_phrase_without_url_anchor() -> None:
    """policy_guidance turns may render 'NCTQ does not take a policy stance...'
    in the deterministic body. The stylist preserves that prose; the anchor
    check should only fire on NEW NCTQ phrasing the stylist added.
    """
    deterministic = (
        "NCTQ does not take a policy stance on collective bargaining.\n\n"
        "| Topic | Coverage |\n| --- | --- |\n| Bargaining | Not addressed |"
    )
    brief = AnswerBrief(
        deterministic_body=deterministic,
        user_question="What is NCTQ's policy stance on collective bargaining?",
        route="policy_guidance",
        result_type="policy_guidance",
        immutable_blocks=(
            ImmutableBlock(
                kind="table",
                text="| Topic | Coverage |\n| --- | --- |\n| Bargaining | Not addressed |",
                block_id="table-1",
            ),
        ),
        allowed_nctq_context=(
            "[rationale] Bargaining stance — Stub.\n"
            "URL: https://www.nctq.org/district-policy-pathfinder/",
        ),
    )
    draft = AnswerDraft(
        body=(
            "NCTQ does not take a policy stance on collective bargaining.\n\n"
            "| Topic | Coverage |\n| --- | --- |\n| Bargaining | Not addressed |"
        )
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is True


def test_validation_accepts_url_anchor_with_trailing_slash_mismatch() -> None:
    """Snippet URL without trailing slash; stylist links with trailing slash.
    Anchor check should normalise."""
    brief = AnswerBrief(
        deterministic_body=(
            "Some data.\n\n| District | Value |\n| --- | --- |\n| Denver | 1 |"
        ),
        user_question="x",
        route="execute",
        result_type="metric_lookup",
        immutable_blocks=(
            ImmutableBlock(
                kind="table",
                text="| District | Value |\n| --- | --- |\n| Denver | 1 |",
                block_id="table-1",
            ),
        ),
        numeric_tokens=("1",),
        allowed_nctq_context=(
            "[rationale] Foo — Bar.\n"
            "URL: https://www.nctq.org/district-policy-pathfinder",
        ),
    )
    draft = AnswerDraft(
        body=(
            "Denver shows 1. NCTQ recommends frontloaded pay "
            "(https://www.nctq.org/district-policy-pathfinder/).\n\n"
            "| District | Value |\n| --- | --- |\n| Denver | 1 |"
        )
    )

    report = validate_answer_draft(brief, draft, mode="gated")

    assert report.accepted is True

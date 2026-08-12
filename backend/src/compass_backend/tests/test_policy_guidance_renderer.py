"""Tests for the pure-Python policy_guidance renderer."""

from __future__ import annotations

import pytest

from compass_backend.contracts import PolicyGuidancePlan
from compass_backend.policy_guidance import (
    ExemplarPolicy,
    PolicyGuidanceLibrary,
    ResearchRationale,
    Stance,
    TopicGuidance,
)
from compass_backend.rendering import policy_guidance as policy_guidance_renderer
from compass_backend.rendering.policy_guidance import render_policy_guidance


# ---------------------------------------------------------------------------
# Fixtures: small library covering single-topic and multi-topic shapes


def _salary_topic() -> TopicGuidance:
    stance = Stance(
        stance_id="stance:general-salary-frontloaded-schedules",
        topic_id="general-salary",
        title="Frontloaded Schedules",
        body="Districts should frontload teacher pay to retain early-career teachers.",
    )
    rationale = ResearchRationale(
        rationale_id="rationale:general-salary-frontloaded-schedules",
        stance_id="stance:general-salary-frontloaded-schedules",
        topic_id="general-salary",
        title="Frontloaded Schedules",
        body="Teacher attrition is highest in the first five years.",
        source_title="NCTQ Research",
        source_url="https://www.nctq.org/district-policy-pathfinder/",  # type: ignore[arg-type]
        citation_status="placeholder",
    )
    exemplar = ExemplarPolicy(
        exemplar_id="exemplar:detroit-frontloaded-salary-schedule",
        topic_id="general-salary",
        district="Detroit Community School District",
        district_id=None,
        subtopic="Annual salary",
        body="Detroit restructured its salary schedule to provide substantial pay increases within the first five years.",
        source_url="https://teacherquality.nctq.org/contract-database/district/detroit",  # type: ignore[arg-type]
        citation_status="ready",
    )
    return TopicGuidance(
        topic_id="general-salary",
        canonical_topic="General Salary",
        aliases=("salary", "starting salary"),
        canonical_url=None,
        topic_brief="Salary brief.",
        stances=(stance,),
        rationales=(rationale,),
        exemplars=(exemplar,),
    )


def _benefits_topic() -> TopicGuidance:
    stance = Stance(
        stance_id="stance:benefits-health-coverage",
        topic_id="benefits",
        title="Health Coverage",
        body="Comprehensive health coverage is a meaningful component of teacher compensation.",
    )
    exemplar = ExemplarPolicy(
        exemplar_id="exemplar:austin-benefits-coverage",
        topic_id="benefits",
        district="Austin ISD",
        district_id=None,
        subtopic="Health benefits",
        body="Austin ISD offers comprehensive health and dental benefits with low employee contribution.",
        source_url="https://teacherquality.nctq.org/contract-database/district/austin",  # type: ignore[arg-type]
        citation_status="ready",
    )
    return TopicGuidance(
        topic_id="benefits",
        canonical_topic="Benefits",
        aliases=("benefits",),
        canonical_url=None,
        topic_brief="Benefits brief.",
        stances=(stance,),
        rationales=(),
        exemplars=(exemplar,),
    )


def _benefits_topic_with_health_exemplars() -> TopicGuidance:
    return TopicGuidance(
        topic_id="benefits",
        canonical_topic="Benefits",
        aliases=("benefits", "health insurance", "tuition reimbursement"),
        canonical_url=None,
        topic_brief="Benefits brief.",
        stances=(),
        rationales=(),
        exemplars=(
            ExemplarPolicy(
                exemplar_id="exemplar:broward-health-premiums",
                topic_id="benefits",
                district="Broward County Public Schools",
                district_id=None,
                subtopic="Health benefits",
                body=(
                    "Broward County covers 100% of employee healthcare premiums "
                    "and has options for dental and vision where employee costs "
                    "are covered."
                ),
                source_url="https://teacherquality.nctq.org/contract-database/district/broward",  # type: ignore[arg-type]
                citation_status="ready",
            ),
            ExemplarPolicy(
                exemplar_id="exemplar:wichita-wellness-discounted-health-insurance",
                topic_id="benefits",
                district="Wichita Public Schools (KS)",
                district_id=None,
                subtopic="Health benefits",
                body=(
                    "Wichita Public Schools offers a wellness-discounted health "
                    "insurance benefit with no-cost coverage incentives."
                ),
                source_url="https://teacherquality.nctq.org/contract-database/district/wichita",  # type: ignore[arg-type]
                citation_status="ready",
            ),
            ExemplarPolicy(
                exemplar_id="exemplar:jackson-affordable-higher-tier-health-plan",
                topic_id="benefits",
                district="Jackson Public Schools (MS)",
                district_id=None,
                subtopic="Health benefits",
                body=(
                    "Jackson Public Schools keeps premiums for the higher-tier "
                    "Select plan affordable."
                ),
                source_url="https://teacherquality.nctq.org/contract-database/district/jackson",  # type: ignore[arg-type]
                citation_status="ready",
            ),
            ExemplarPolicy(
                exemplar_id="exemplar:tuition-reimbursement-example",
                topic_id="benefits",
                district="Example Tuition District",
                district_id=None,
                subtopic="Tuition reimbursement",
                body="Example Tuition District reimburses teachers for coursework.",
                source_url="https://teacherquality.nctq.org/contract-database/district/example",  # type: ignore[arg-type]
                citation_status="ready",
            ),
        ),
    )


def _leave_topic_with_mixed_exemplars() -> TopicGuidance:
    return TopicGuidance(
        topic_id="leave",
        canonical_topic="Leave",
        aliases=("leave", "parental leave"),
        canonical_url=None,
        topic_brief="Leave brief.",
        stances=(
            Stance(
                stance_id="stance:leave-parental-leave",
                topic_id="leave",
                title="Parental Leave",
                body="Districts should provide paid parental leave.",
            ),
        ),
        rationales=(),
        exemplars=(
            ExemplarPolicy(
                exemplar_id="exemplar:anchorage-paid-personal-sick-days",
                topic_id="leave",
                district="Anchorage School District",
                district_id=None,
                subtopic="Personal and sick days",
                body="Anchorage provides flexible personal and sick days.",
                source_url="https://teacherquality.nctq.org/contract-database/district/anchorage",  # type: ignore[arg-type]
                citation_status="ready",
            ),
            ExemplarPolicy(
                exemplar_id="exemplar:red-clay-delaware-parental-leave",
                topic_id="leave",
                district="Red Clay Consolidated School District",
                district_id=None,
                subtopic="Paid parental leave",
                body="Red Clay provides 12 weeks of paid parental leave.",
                source_url="https://teacherquality.nctq.org/contract-database/district/red-clay",  # type: ignore[arg-type]
                citation_status="ready",
            ),
            ExemplarPolicy(
                exemplar_id="exemplar:wake-county-parental-leave",
                topic_id="leave",
                district="Wake County School District",
                district_id=None,
                subtopic="Paid parental leave",
                body="Wake County offers paid parental leave for new parents.",
                source_url="https://teacherquality.nctq.org/contract-database/district/wake-county",  # type: ignore[arg-type]
                citation_status="ready",
            ),
        ),
    )


def _differentiated_pay_topic_with_mixed_exemplars() -> TopicGuidance:
    return TopicGuidance(
        topic_id="differentiated-pay",
        canonical_topic="Differentiated Pay",
        aliases=("differentiated pay", "performance pay"),
        canonical_url=None,
        topic_brief="Differentiated pay brief.",
        stances=(
            Stance(
                stance_id="stance:differentiated-pay-performance-pay",
                topic_id="differentiated-pay",
                title="Performance Pay",
                body="Districts should consider substantial performance pay.",
            ),
        ),
        rationales=(),
        exemplars=(
            ExemplarPolicy(
                exemplar_id="exemplar:hawaii-special-education-supplement",
                topic_id="differentiated-pay",
                district="Hawaii Department of Education",
                district_id=None,
                subtopic="Special education supplement",
                body="Hawaii offers special education supplements.",
                source_url="https://teacherquality.nctq.org/contract-database/district/hawaii",  # type: ignore[arg-type]
                citation_status="ready",
            ),
            ExemplarPolicy(
                exemplar_id="exemplar:dcps-performance-pay",
                topic_id="differentiated-pay",
                district="District of Columbia Public Schools",
                district_id=None,
                subtopic="Performance pay",
                body="DCPS links additional compensation to teacher performance.",
                source_url="https://teacherquality.nctq.org/contract-database/district/dcps",  # type: ignore[arg-type]
                citation_status="ready",
            ),
        ),
    )


def _library() -> PolicyGuidanceLibrary:
    return PolicyGuidanceLibrary.build(
        topics={"general-salary": _salary_topic(), "benefits": _benefits_topic()},
    )


def _plan(**overrides: object) -> PolicyGuidancePlan:
    base: dict[str, object] = {
        "topic_ids": ["general-salary"],
        "layers": ["exemplars"],
        "intent_summary": "User asked for an exemplary district on salary policy.",
    }
    base.update(overrides)
    return PolicyGuidancePlan(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Single-topic, single-layer — Natalie's happy path


def test_exemplars_only_includes_district_and_citation_marker() -> None:
    """Natalie's prompt route: 'show me a district that does this well' →
    exemplars layer with Detroit + frontend-compatible numeric marker."""
    plan = _plan()
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    assert manifest.validation_valid
    assert manifest.result_type == "policy_guidance"
    body = manifest.body
    assert "Detroit Community School District" in body
    assert "[1]" in body
    assert "[exemplar:detroit-frontloaded-salary-schedule]" not in body
    assert "first five years" in body


def test_exemplars_only_leads_with_grounded_strong_policy_framing() -> None:
    """#1626 §1: a 'best/strongest <topic>' answer (exemplars layer only) leads
    with a grounded one-line framing of what NCTQ looks for in a strong policy —
    drawn from the topic's stance titles — before the example district, instead
    of jumping straight to the list. Grounded: no criteria are invented."""
    plan = _plan()  # exemplars-only, general-salary
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"  # the framing passes policy-guidance validation
    body = manifest.body
    assert "What NCTQ looks for in a strong general salary policy:" in body
    # Grounded in the topic's actual stance title — nothing invented.
    assert "Frontloaded Schedules" in body
    # Leads: the framing precedes the exemplar district.
    assert body.index("What NCTQ looks for in a strong") < body.index(
        "Detroit Community School District"
    )


def test_exemplars_only_does_not_include_misleading_database_framing() -> None:
    """The fabricated-table bug stamped 'Here's what we have in the database'
    on hallucinated content. Confirm the new renderer never emits that string."""
    plan = _plan()
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert "Here's what we have in the database" not in manifest.body


def test_stances_only_renders_with_stance_marker() -> None:
    plan = _plan(layers=["stances"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert "Frontloaded Schedules" in manifest.body
    assert "[1]" in manifest.body
    assert "[stance:general-salary-frontloaded-schedules]" not in manifest.body


def test_rationales_only_renders_with_rationale_marker() -> None:
    plan = _plan(layers=["rationales"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert "Teacher attrition" in manifest.body
    assert "[1]" in manifest.body
    assert "[rationale:general-salary-frontloaded-schedules]" not in manifest.body


# ---------------------------------------------------------------------------
# Single-topic, multi-layer — full picture


def test_multi_layer_orders_stances_rationales_exemplars() -> None:
    """When the user asks for the full picture, render order matters: NCTQ
    position → research basis → exemplary district. This is the connective
    tissue Natalie described in her 2026-05-07 call."""
    plan = _plan(layers=["stances", "rationales", "exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    body = manifest.body
    # All three layer sections render in order. Marker numbers can collapse
    # when stance and rationale share the Pathfinder placeholder URL (the
    # _salary_topic fixture exercises that case), so order is checked by
    # heading position rather than by [N] indices — #867.
    assert "NCTQ's Position" in body
    assert "Research Basis" in body
    assert "Exemplary Districts" in body
    stance_pos = body.index("NCTQ's Position")
    rationale_pos = body.index("Research Basis")
    exemplar_pos = body.index("Exemplary Districts")
    assert stance_pos < rationale_pos < exemplar_pos


def test_intent_summary_stays_metadata_not_user_facing_prose() -> None:
    plan = _plan(intent_summary="Exemplary district on early-career salary.")
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert "Exemplary district on early-career salary." not in manifest.body
    assert manifest.metadata["intent_summary"] == "Exemplary district on early-career salary."
    # _salary_topic has one exemplar (Detroit) → singular intro per #936;
    # general-salary leads with the #1613 subjectivity frame.
    assert manifest.body.startswith('There\'s no single "best" on teacher pay')
    assert "Here is one NCTQ-curated exemplary district policy" in manifest.body


def test_single_exemplar_intro_uses_singular_framing() -> None:
    """#936: when only one exemplar survives bundle filters, the intro must
    say 'one' rather than the plural 'policies' framing — the latter trips
    the uncertainty_acknowledgment judge by reading as a comprehensive list."""
    plan = _plan(layers=["exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    # Sanity: the salary topic ships with exactly one exemplar (Detroit).
    assert len(bundle.exemplars) == 1
    manifest = render_policy_guidance(plan, bundle, library=library)
    # #1613: general-salary exemplars lead with the subjectivity frame; the
    # #936 singular "one" cue is preserved immediately after it.
    assert manifest.body.startswith(
        'There\'s no single "best" on teacher pay — it depends what you value. '
        "Here is one NCTQ-curated exemplary district policy for General Salary."
    )


def test_multi_exemplar_intro_keeps_plural_framing() -> None:
    """#936 regression guard: multi-exemplar bundles must still use the
    plural intro so the change in singular-handling does not over-correct."""
    library = PolicyGuidanceLibrary.build(
        topics={"benefits": _benefits_topic_with_health_exemplars()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["benefits"],
        layers=["exemplars"],
        intent_summary="Three health-benefits exemplars (regression guard).",
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    assert len(bundle.exemplars) >= 2
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert manifest.body.startswith(
        "Here are NCTQ-curated exemplary district policies for Benefits."
    )


# ---------------------------------------------------------------------------
# Multi-topic


def test_multi_topic_groups_by_topic_with_heading() -> None:
    plan = _plan(
        topic_ids=["general-salary", "benefits"],
        layers=["exemplars"],
        intent_summary="Salary AND benefits exemplars.",
    )
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    body = manifest.body
    assert "## General Salary" in body or "### General Salary" in body
    assert "## Benefits" in body or "### Benefits" in body
    assert "Detroit Community School District" in body
    assert "Austin ISD" in body


def test_primary_topic_id_is_rendered_first() -> None:
    plan = _plan(
        topic_ids=["general-salary", "benefits"],
        layers=["exemplars"],
        primary_topic_id="benefits",
        intent_summary="Benefits-first.",
    )
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    # Benefits content should appear before Salary content
    benefits_pos = manifest.body.find("Austin ISD")
    salary_pos = manifest.body.find("Detroit")
    assert 0 <= benefits_pos < salary_pos


def test_focus_terms_filter_leave_exemplars_to_parental_leave() -> None:
    """Scenario 139: parental leave requests must not return every leave exemplar."""
    library = PolicyGuidanceLibrary.build(topics={"leave": _leave_topic_with_mixed_exemplars()})
    plan = PolicyGuidancePlan(
        topic_ids=["leave"],
        layers=["exemplars"],
        intent_summary="User asked for exemplary parental leave policies.",
        focus_terms=["parental leave"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    assert "Red Clay Consolidated School District" in manifest.body
    assert "Wake County School District" in manifest.body
    assert "Anchorage School District" not in manifest.body
    citation_titles = [c["title"] for c in manifest.metadata["citations"]]
    assert citation_titles == [
        "Red Clay Consolidated School District — Paid parental leave",
        "Wake County School District — Paid parental leave",
    ]
    assert manifest.metadata["focus_terms"] == ["parental leave"]


def test_focus_terms_filter_benefits_exemplars_to_health_benefits() -> None:
    library = PolicyGuidanceLibrary.build(
        topics={"benefits": _benefits_topic_with_health_exemplars()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["benefits"],
        layers=["exemplars"],
        intent_summary="User asked for health benefits exemplars.",
        focus_terms=["health benefits"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    assert manifest.validation_valid
    assert manifest.body.startswith(
        "Here are NCTQ-curated exemplary district policies for Health Benefits."
    )
    assert "Broward County Public Schools" in manifest.body
    assert "Wichita Public Schools (KS)" in manifest.body
    assert "Jackson Public Schools (MS)" in manifest.body
    assert "Example Tuition District" not in manifest.body
    assert "[1]" in manifest.body
    assert "[2]" in manifest.body
    assert "[3]" in manifest.body
    citation_titles = [c["title"] for c in manifest.metadata["citations"]]
    assert citation_titles == [
        "Broward County Public Schools — Health benefits",
        "Wichita Public Schools (KS) — Health benefits",
        "Jackson Public Schools (MS) — Health benefits",
    ]
    assert manifest.metadata["focus_terms"] == ["health benefits"]


def test_focus_terms_intro_uses_subtopic_label_not_parent_topic() -> None:
    """#737 PR-3C: when focus_terms narrows the bundle, the rendered intro
    heading must say "...for Parental Leave." (subtopic), not "...for Leave."
    (parent canonical topic). The exemplar selection was already correctly
    narrowed (see test_focus_terms_filter_leave_exemplars_to_parental_leave);
    this asserts the renderer heading matches that narrowing.
    """
    library = PolicyGuidanceLibrary.build(topics={"leave": _leave_topic_with_mixed_exemplars()})
    plan = PolicyGuidancePlan(
        topic_ids=["leave"],
        layers=["exemplars"],
        intent_summary="User asked for exemplary parental leave policies.",
        focus_terms=["parental leave"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert "Parental Leave" in manifest.body
    # The heading must NOT use the parent topic label when narrowed.
    assert "policies for Leave" not in manifest.body


def test_focus_terms_intro_uses_subtopic_label_for_performance_pay() -> None:
    """#737 PR-3C: "Differentiated Pay" parent topic narrowed by focus_terms=
    ["performance pay"] must render heading as "...for Performance Pay." not
    "...for Differentiated Pay."
    """
    library = PolicyGuidanceLibrary.build(
        topics={"differentiated-pay": _differentiated_pay_topic_with_mixed_exemplars()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["differentiated-pay"],
        layers=["exemplars"],
        intent_summary="User asked for performance pay exemplars.",
        focus_terms=["performance pay"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert "Performance Pay" in manifest.body
    assert "policies for Differentiated Pay" not in manifest.body


def test_intro_falls_back_to_canonical_topic_when_no_focus_terms() -> None:
    """#737 PR-3C: broad topic requests (no focus_terms) preserve the existing
    behavior — the heading uses the canonical topic label.
    """
    library = PolicyGuidanceLibrary.build(topics={"leave": _leave_topic_with_mixed_exemplars()})
    plan = PolicyGuidancePlan(
        topic_ids=["leave"],
        layers=["exemplars"],
        intent_summary="User asked for broad leave guidance.",
        focus_terms=[],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    # Canonical heading preserved for broad requests.
    assert "policies for Leave" in manifest.body


def test_focus_terms_filter_differentiated_pay_to_performance_pay() -> None:
    """Scenarios 250/253: performance-pay focus should not return all differentiated pay."""
    library = PolicyGuidanceLibrary.build(
        topics={"differentiated-pay": _differentiated_pay_topic_with_mixed_exemplars()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["differentiated-pay"],
        layers=["exemplars"],
        intent_summary="User asked for performance pay exemplars.",
        focus_terms=["performance pay"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert "District of Columbia Public Schools" in manifest.body
    assert "Hawaii Department of Education" not in manifest.body
    assert manifest.validation_valid


def test_exemplar_detail_filters_selected_exemplar_ids() -> None:
    """Case 354: follow-up detail turns render the selected approved exemplar only."""
    library = PolicyGuidanceLibrary.build(
        topics={"differentiated-pay": _differentiated_pay_topic_with_mixed_exemplars()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["differentiated-pay"],
        layers=["exemplars"],
        intent_summary="User asked for performance pay exemplar details.",
        focus_terms=["performance pay"],
        selected_exemplar_ids=["exemplar:dcps-performance-pay"],
        response_mode="exemplar_detail",
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    assert manifest.validation_valid
    assert manifest.body.startswith(
        "Here are the approved policy details for District of Columbia Public Schools"
    )
    assert "DCPS links additional compensation to teacher performance." in manifest.body
    assert "Hawaii Department of Education" not in manifest.body
    assert manifest.metadata["selected_exemplar_ids"] == [
        "exemplar:dcps-performance-pay"
    ]
    assert manifest.metadata["response_mode"] == "exemplar_detail"
    assert [c["stable_id"] for c in manifest.metadata["citations"]] == [
        "exemplar:dcps-performance-pay"
    ]
    assert manifest.metadata["citations"][0]["district"] == (
        "District of Columbia Public Schools"
    )
    assert manifest.metadata["citations"][0]["subtopic"] == "Performance pay"


def test_focus_terms_empty_match_returns_honest_empty_response() -> None:
    """A narrowed request with no matching content must not expose unrelated examples."""
    library = PolicyGuidanceLibrary.build(topics={"leave": _leave_topic_with_mixed_exemplars()})
    plan = PolicyGuidancePlan(
        topic_ids=["leave"],
        layers=["exemplars"],
        intent_summary="User asked for a strike legality exemplar.",
        focus_terms=["strike legality"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert "I couldn't find NCTQ exemplars content" in manifest.body
    assert "Anchorage School District" not in manifest.body
    assert "Red Clay Consolidated School District" not in manifest.body
    assert manifest.metadata["citations"] == []
    assert manifest.metadata["focus_terms"] == ["strike legality"]


# ---------------------------------------------------------------------------
# Edge cases


def test_empty_bundle_returns_non_empty_warning_response() -> None:
    """When library.assemble returns nothing (e.g., topic has no exemplars
    and layers=['exemplars']), render an honest fallback — not silence."""
    leave_topic = TopicGuidance(
        topic_id="leave",
        canonical_topic="Leave",
        aliases=("leave",),
        canonical_url=None,
        topic_brief="brief",
        stances=(
            Stance(
                stance_id="stance:leave-paid",
                topic_id="leave",
                title="Paid Leave",
                body="Leave body.",
            ),
        ),
        rationales=(),
        exemplars=(),  # no exemplars on this topic
    )
    library = PolicyGuidanceLibrary.build(topics={"leave": leave_topic})
    plan = PolicyGuidancePlan(
        topic_ids=["leave"],
        layers=["exemplars"],
        intent_summary="User asked for a leave exemplar.",
    )
    bundle = library.assemble(topic_ids=["leave"], layers=["exemplars"])
    manifest = render_policy_guidance(plan, bundle, library=library)
    # Body is non-empty and tells the user honestly
    assert len(manifest.body) > 0
    assert manifest.status == "rendered"
    assert manifest.warnings  # at least one warning recorded


def test_metadata_records_plan_for_telemetry() -> None:
    plan = _plan(
        topic_ids=["general-salary", "benefits"],
        layers=["stances", "exemplars"],
        primary_topic_id="benefits",
    )
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert manifest.metadata["topic_ids"] == ["general-salary", "benefits"]
    assert manifest.metadata["layers"] == ["stances", "exemplars"]
    assert manifest.metadata["primary_topic_id"] == "benefits"
    assert manifest.metadata["intent_summary"] == plan.intent_summary


def test_citations_index_in_metadata() -> None:
    """The Sources panel + frontend marker resolver read citations from
    manifest.metadata. Each citation should carry the stable ID, source URL,
    and a human-readable title."""
    plan = _plan(layers=["exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    citations = manifest.metadata["citations"]
    assert isinstance(citations, list)
    assert len(citations) == 1
    citation = citations[0]
    assert citation["id"] == 1
    assert citation["marker"] == "[1]"
    assert citation["stable_id"] == "exemplar:detroit-frontloaded-salary-schedule"
    assert citation["url"] == "https://teacherquality.nctq.org/contract-database/district/detroit"
    assert "Detroit" in citation["title"]


def test_citations_include_status_and_provenance() -> None:
    plan = _plan(layers=["stances", "rationales", "exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    citations = {citation["stable_id"]: citation for citation in manifest.metadata["citations"]}

    stance = citations["stance:general-salary-frontloaded-schedules"]
    assert stance["id"] == 1
    assert stance["marker"] == "[1]"
    assert stance["citation_status"] == "placeholder"
    assert stance["citation_type"] == "stance"
    assert stance["topic_id"] == "general-salary"
    assert stance["source_kind"] == "topic_placeholder"

    # #867: the rationale's source_url is the same Pathfinder placeholder URL
    # the stance carries, so it dedupes into the first-seen stance entry —
    # the rationale's stable_id appears in the body via the alias map but no
    # second Sources entry is emitted (one URL → one Sources row, per
    # execution/evidence.py:_citation_identity / PR #868 / #748).
    assert "rationale:general-salary-frontloaded-schedules" not in citations

    exemplar = citations["exemplar:detroit-frontloaded-salary-schedule"]
    assert exemplar["id"] == 2
    assert exemplar["marker"] == "[2]"
    assert exemplar["citation_status"] == "ready"
    assert exemplar["citation_type"] == "exemplar"
    assert exemplar["topic_id"] == "general-salary"
    assert exemplar["source_kind"] == "district_exemplar"


def test_citations_dedup_same_url_within_one_topic_across_layers() -> None:
    """#867: stance + rationale that point at the same URL collapse into one
    Sources entry. The _salary_topic fixture has both at PATHFINDER_PLACEHOLDER
    by design, so two bullets render under [1] and the exemplar (distinct URL)
    moves to [2]."""
    plan = _plan(layers=["stances", "rationales", "exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    citations = manifest.metadata["citations"]
    urls = [str(c["url"]) for c in citations]
    assert len(set(urls)) == len(urls), "duplicate URLs leaked into Sources panel"
    assert len(citations) == 2  # stance + rationale collapse; exemplar remains
    assert "[1]" in manifest.body
    assert "[2]" in manifest.body
    # Both stance and rationale bullets reference the merged [1] — assert by
    # finding the rationale body text (from the fixture at line 35) followed
    # by [1] rather than a fresh marker.
    rationale_bullet = manifest.body[
        manifest.body.index("Teacher attrition is highest"):
    ]
    rationale_marker = rationale_bullet.split("\n", 1)[0]
    assert "[1]" in rationale_marker
    assert "[2]" not in rationale_marker  # not its own marker


def test_citations_dedup_same_url_across_topics() -> None:
    """#867: two topics whose exemplars cite the same district URL produce one
    Sources entry. This mirrors the cross-renderer dedup landed in PR #868 for
    composite ranking."""
    shared_url = "https://teacherquality.nctq.org/contract-database/district/shared"
    topic_a = TopicGuidance(
        topic_id="topic-a",
        canonical_topic="Topic A",
        aliases=(),
        canonical_url=None,
        topic_brief="A brief.",
        stances=(
            Stance(
                stance_id="stance:topic-a",
                topic_id="topic-a",
                title="Topic A stance",
                body="A stance body.",
            ),
        ),
        rationales=(),
        exemplars=(
            ExemplarPolicy(
                exemplar_id="exemplar:topic-a-shared",
                topic_id="topic-a",
                district="Shared District",
                district_id=None,
                subtopic="Subtopic A",
                body="Shared district covers subtopic A.",
                source_url=shared_url,  # type: ignore[arg-type]
                citation_status="ready",
            ),
        ),
    )
    topic_b = TopicGuidance(
        topic_id="topic-b",
        canonical_topic="Topic B",
        aliases=(),
        canonical_url=None,
        topic_brief="B brief.",
        stances=(
            Stance(
                stance_id="stance:topic-b",
                topic_id="topic-b",
                title="Topic B stance",
                body="B stance body.",
            ),
        ),
        rationales=(),
        exemplars=(
            ExemplarPolicy(
                exemplar_id="exemplar:topic-b-shared",
                topic_id="topic-b",
                district="Shared District",
                district_id=None,
                subtopic="Subtopic B",
                body="Shared district covers subtopic B.",
                source_url=shared_url,  # type: ignore[arg-type]
                citation_status="ready",
            ),
        ),
    )
    library = PolicyGuidanceLibrary.build(
        topics={"topic-a": topic_a, "topic-b": topic_b},
    )
    plan = PolicyGuidancePlan(
        topic_ids=["topic-a", "topic-b"],
        layers=["exemplars"],
        primary_topic_id="topic-a",
        intent_summary="Cross-topic shared-URL fixture for #867.",
    )
    bundle = library.assemble(
        topic_ids=list(plan.topic_ids), layers=list(plan.layers)
    )
    manifest = render_policy_guidance(plan, bundle, library=library)

    citations = manifest.metadata["citations"]
    urls = [str(c["url"]) for c in citations]
    assert urls.count(shared_url) == 1, "shared URL must appear once in Sources"
    assert len(citations) == 1
    assert citations[0]["marker"] == "[1]"
    # Both exemplar bullets resolve to [1] via the alias map.
    assert manifest.body.count("[1]") == 2
    assert "[2]" not in manifest.body


def test_renderer_validation_fails_when_body_marker_has_no_metadata_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(layers=["exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    monkeypatch.setattr(
        policy_guidance_renderer,
        "_build_citation_index",
        lambda *args, **kwargs: ([], {}, frozenset()),
    )

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "validation_failed"
    assert manifest.validation_valid is False
    assert any("missing_metadata_citation" in warning for warning in manifest.warnings)


def test_renderer_validation_fails_when_metadata_citation_not_in_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(layers=["exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    monkeypatch.setattr(
        policy_guidance_renderer,
        "_build_citation_index",
        lambda *args, **kwargs: (
            [
                {
                    "id": 99,
                    "marker": "[99]",
                    "stable_id": "exemplar:not-rendered",
                    "url": "https://teacherquality.nctq.org/contract-database/district/nowhere",
                    "title": "Not rendered",
                    "citation_status": "ready",
                    "citation_type": "exemplar",
                    "topic_id": "general-salary",
                    "source_kind": "district_exemplar",
                }
            ],
            {"exemplar:not-rendered": "[99]"},
            frozenset(),
        ),
    )

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "validation_failed"
    assert manifest.validation_valid is False
    assert any("metadata_citation_not_in_body" in warning for warning in manifest.warnings)


def test_renderer_validation_fails_when_citation_status_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(layers=["exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    monkeypatch.setattr(
        policy_guidance_renderer,
        "_build_citation_index",
        lambda *args, **kwargs: (
            [
                {
                    "id": 1,
                    "marker": "[1]",
                    "stable_id": "exemplar:detroit-frontloaded-salary-schedule",
                    "url": "https://teacherquality.nctq.org/contract-database/district/detroit",
                    "title": "Detroit",
                    "citation_type": "exemplar",
                    "topic_id": "general-salary",
                    "source_kind": "district_exemplar",
                }
            ],
            {"exemplar:detroit-frontloaded-salary-schedule": "[1]"},
            frozenset(),
        ),
    )

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "validation_failed"
    assert manifest.validation_valid is False
    assert any("missing_citation_status" in warning for warning in manifest.warnings)


def test_no_llm_call_inside_renderer() -> None:
    """Pure-Python — no awaitable, no async, no agent dependency. Smoke test
    that the function signature is sync and produces output deterministically."""
    plan = _plan()
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    # Two consecutive calls produce identical bodies
    a = render_policy_guidance(plan, bundle, library=library)
    b = render_policy_guidance(plan, bundle, library=library)
    assert a.body == b.body


# ---------------------------------------------------------------------------
# W2-M5-00b (#865) — visible [provisional source] marker on placeholder rows


def _salary_topic_with_canonical_url() -> TopicGuidance:
    """Variant of _salary_topic with a real canonical_url so the stance and
    rationale render as 'ready' citations rather than the Pathfinder placeholder."""
    stance = Stance(
        stance_id="stance:general-salary-frontloaded-schedules",
        topic_id="general-salary",
        title="Frontloaded Schedules",
        body="Districts should frontload teacher pay to retain early-career teachers.",
    )
    rationale = ResearchRationale(
        rationale_id="rationale:general-salary-frontloaded-schedules",
        stance_id="stance:general-salary-frontloaded-schedules",
        topic_id="general-salary",
        title="Frontloaded Schedules",
        body="Teacher attrition is highest in the first five years.",
        source_title="NCTQ Research",
        source_url="https://www.nctq.org/district-policy-pathfinder/general-salary",  # type: ignore[arg-type]
        citation_status="ready",
    )
    exemplar = ExemplarPolicy(
        exemplar_id="exemplar:detroit-frontloaded-salary-schedule",
        topic_id="general-salary",
        district="Detroit Community School District",
        district_id=None,
        subtopic="Annual salary",
        body="Detroit restructured its salary schedule to provide substantial pay increases.",
        source_url="https://teacherquality.nctq.org/contract-database/district/detroit",  # type: ignore[arg-type]
        citation_status="ready",
    )
    return TopicGuidance(
        topic_id="general-salary",
        canonical_topic="General Salary",
        aliases=("salary",),
        canonical_url="https://www.nctq.org/district-policy-pathfinder/general-salary",  # type: ignore[arg-type]
        topic_brief="Salary brief.",
        stances=(stance,),
        rationales=(rationale,),
        exemplars=(exemplar,),
    )


def test_placeholder_stance_bullet_carries_provisional_source_marker() -> None:
    """A stance whose topic has no canonical_url renders as a placeholder
    citation. Per W2-M5-00b (#865): the bullet body must show
    `[provisional source]` so the reader knows the link is a stopgap."""
    plan = _plan(layers=["stances"])
    library = _library()  # general-salary has canonical_url=None → stance is placeholder
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    assert "[provisional source]" in manifest.body
    # Sources panel metadata still records the placeholder status (unchanged).
    statuses = {c["citation_status"] for c in manifest.metadata["citations"]}
    assert "placeholder" in statuses


def test_ready_stance_bullet_does_not_carry_provisional_source_marker() -> None:
    """A stance whose topic has a real canonical_url is a `ready` citation.
    The provisional marker must NOT appear on its bullet."""
    library = PolicyGuidanceLibrary.build(
        topics={"general-salary": _salary_topic_with_canonical_url()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["general-salary"],
        layers=["stances"],
        intent_summary="Stances with a real canonical_url.",
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    assert "[provisional source]" not in manifest.body
    statuses = {c["citation_status"] for c in manifest.metadata["citations"]}
    assert statuses == {"ready"}


def test_placeholder_rationale_bullet_carries_provisional_source_marker() -> None:
    """A rationale whose `citation_status='placeholder'` (Pathfinder homepage
    URL) must show the visible `[provisional source]` marker on its bullet."""
    plan = _plan(layers=["rationales"])
    library = _library()  # salary rationale citation_status='placeholder'
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    assert "[provisional source]" in manifest.body
    # Body still contains the substantive rationale prose.
    assert "Teacher attrition" in manifest.body


def test_ready_exemplar_bullet_does_not_carry_provisional_source_marker() -> None:
    """The Detroit exemplar in the salary fixture has `citation_status='ready'`
    and a real teacherquality.nctq.org URL — the bullet must NOT show the
    provisional marker."""
    plan = _plan(layers=["exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    assert "Detroit Community School District" in manifest.body
    assert "[provisional source]" not in manifest.body


def test_placeholder_exemplar_bullet_carries_provisional_source_marker() -> None:
    """Symmetry with the stance/rationale placeholder tests: an exemplar
    whose `citation_status='placeholder'` (Pathfinder homepage URL) must
    also show `[provisional source]` on its bullet."""
    stance = Stance(
        stance_id="stance:benefits-placeholder",
        topic_id="benefits",
        title="Comprehensive Benefits",
        body="Districts should offer comprehensive benefits.",
    )
    placeholder_exemplar = ExemplarPolicy(
        exemplar_id="exemplar:benefits-placeholder",
        topic_id="benefits",
        district="Placeholder District",
        district_id=None,
        subtopic="Benefits",
        body="An example district policy without a published source page.",
        source_url="https://www.nctq.org/district-policy-pathfinder/",  # type: ignore[arg-type]
        citation_status="placeholder",
    )
    topic = TopicGuidance(
        topic_id="benefits",
        canonical_topic="Benefits",
        aliases=("benefits",),
        canonical_url=None,
        topic_brief="Benefits brief.",
        stances=(stance,),
        rationales=(),
        exemplars=(placeholder_exemplar,),
    )
    library = PolicyGuidanceLibrary.build(topics={"benefits": topic})
    plan = PolicyGuidancePlan(
        topic_ids=["benefits"],
        layers=["exemplars"],
        intent_summary="Placeholder exemplar render.",
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    exemplar_line = next(
        line for line in manifest.body.splitlines() if "Placeholder District" in line
    )
    assert "[provisional source]" in exemplar_line


def test_ready_rationale_bullet_does_not_carry_provisional_source_marker() -> None:
    """Symmetry with the ready-stance negative test: rationale rows whose
    `citation_status='ready'` must not show the provisional marker."""
    library = PolicyGuidanceLibrary.build(
        topics={"general-salary": _salary_topic_with_canonical_url()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["general-salary"],
        layers=["rationales"],
        intent_summary="Rationales-only render with a ready citation.",
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    assert "[provisional source]" not in manifest.body
    statuses = {c["citation_status"] for c in manifest.metadata["citations"]}
    assert statuses == {"ready"}


def test_mixed_bundle_marks_only_placeholder_rows() -> None:
    """Multi-layer salary render: stance (placeholder) + rationale (placeholder)
    + exemplar (ready). The provisional marker must appear exactly twice —
    once on the stance bullet, once on the rationale bullet — and never on
    the Detroit exemplar bullet."""
    plan = _plan(layers=["stances", "rationales", "exemplars"])
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))

    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid
    body = manifest.body
    assert body.count("[provisional source]") == 2
    # Locate the Detroit bullet (single line in markdown) and confirm the
    # marker isn't on it.
    detroit_line = next(
        line for line in body.splitlines() if "Detroit" in line
    )
    assert "[provisional source]" not in detroit_line


# ---------------------------------------------------------------------------
# Governed regional refinement of exemplars (#1360 — "narrow to the South")


def _exemplar(slug: str, district: str, state: str | None) -> ExemplarPolicy:
    return ExemplarPolicy(
        exemplar_id=f"exemplar:{slug}",
        topic_id="differentiated-pay",
        district=district,
        district_id=None,
        state=state,
        subtopic="Performance pay",
        body=f"{district} runs a model performance-pay program.",
        # Unique per district so the citation index doesn't dedup them by URL.
        source_url=f"https://teacherquality.nctq.org/contract-database/district/{slug}",  # type: ignore[arg-type]
        citation_status="ready",
    )


def _multi_state_pay_topic() -> TopicGuidance:
    """Exemplars spanning South (DC, TX), non-South (HI), and unknown state."""
    return TopicGuidance(
        topic_id="differentiated-pay",
        canonical_topic="Differentiated Pay",
        aliases=("performance pay",),
        canonical_url=None,
        topic_brief="Differentiated pay brief.",
        stances=(),
        rationales=(),
        exemplars=(
            _exemplar("dc-perf", "District of Columbia Public Schools", "DC"),
            _exemplar("dallas-perf", "Dallas Independent School District", "TX"),
            _exemplar("hawaii-perf", "Hawaii Department of Education", "HI"),
            _exemplar("unknown-perf", "Mystery School District", None),
        ),
    )


def _pay_library() -> PolicyGuidanceLibrary:
    return PolicyGuidanceLibrary.build(
        topics={"differentiated-pay": _multi_state_pay_topic()}
    )


def _pay_plan(**overrides: object) -> PolicyGuidancePlan:
    base: dict[str, object] = {
        "topic_ids": ["differentiated-pay"],
        "layers": ["exemplars"],
        "intent_summary": "Exemplary differentiated-pay districts.",
    }
    base.update(overrides)
    return PolicyGuidancePlan(**base)  # type: ignore[arg-type]


def test_region_south_keeps_only_southern_exemplars_and_states_it() -> None:
    library = _pay_library()
    plan = _pay_plan(region="the South")
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    body = manifest.body
    # Governed South includes DC + TX, excludes HI and the unknown-state row.
    assert "District of Columbia Public Schools" in body
    assert "Dallas Independent School District" in body
    assert "Hawaii Department of Education" not in body
    assert "Mystery School District" not in body
    # The geographic filter is stated explicitly, not silent.
    assert "in the South" in body
    assert manifest.metadata["region"] == "the South"


def test_region_filter_matches_governed_census_south() -> None:
    """Renderer region expansion must equal the data path's governed set."""
    from compass_backend.reference import CENSUS_SOUTH_STATES

    library = _pay_library()
    plan = _pay_plan(region="the South")
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    # DC and TX are governed-South; the kept districts must be exactly those.
    assert "DC" in CENSUS_SOUTH_STATES and "TX" in CENSUS_SOUTH_STATES
    assert "HI" not in CENSUS_SOUTH_STATES
    kept = {c["district"] for c in manifest.metadata["citations"]}
    assert kept == {
        "District of Columbia Public Schools",
        "Dallas Independent School District",
    }


def test_no_region_keeps_all_exemplars() -> None:
    library = _pay_library()
    plan = _pay_plan()  # no region
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert "Hawaii Department of Education" in manifest.body
    assert "Mystery School District" in manifest.body
    assert "in the South" not in manifest.body
    assert manifest.metadata["region"] is None


def test_region_with_no_in_region_exemplar_emits_honest_miss() -> None:
    """A region the curated set can't satisfy yields a region-aware honest
    message, not the generic 'couldn't find content' data-gap framing."""
    library = PolicyGuidanceLibrary.build(
        topics={
            "differentiated-pay": TopicGuidance(
                topic_id="differentiated-pay",
                canonical_topic="Differentiated Pay",
                aliases=(),
                canonical_url=None,
                topic_brief="brief.",
                stances=(),
                rationales=(),
                exemplars=(_exemplar("hi-only", "Hawaii Department of Education", "HI"),),
            )
        }
    )
    plan = _pay_plan(region="the South")
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert "doesn't have a curated exemplary district policy in the South" in manifest.body
    assert "Differentiated Pay" in manifest.body  # canonical name, not the slug
    assert any(w.startswith("no_region_match") for w in manifest.warnings)


def test_region_west_keeps_western_exemplar_and_states_it() -> None:
    """A non-South governed region works the same way: the West keeps Hawaii
    (HI) and drops the Southern districts, naming the region."""
    library = _pay_library()
    plan = _pay_plan(region="the West")
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    body = manifest.body
    assert "Hawaii Department of Education" in body
    assert "District of Columbia Public Schools" not in body
    assert "Dallas Independent School District" not in body
    assert "in the West" in body
    assert manifest.metadata["region"] == "the West"


def test_governed_region_with_no_member_exemplar_yields_no_match() -> None:
    """A governed region the curated set doesn't cover (no Midwest district in
    this fixture) still fires the honest miss rather than an unfiltered set."""
    library = _pay_library()
    plan = _pay_plan(region="the Midwest")
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert any(w.startswith("no_region_match") for w in manifest.warnings)
    assert "Dallas Independent School District" not in manifest.body


def test_ungoverned_region_phrase_yields_no_match() -> None:
    """A phrase outside the four Census regions ("Appalachia") can't match any
    exemplar, so the honest miss fires rather than an unfiltered set."""
    library = _pay_library()
    plan = _pay_plan(region="Appalachia")
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)
    assert any(w.startswith("no_region_match") for w in manifest.warnings)
    assert "Dallas Independent School District" not in manifest.body


# ---------------------------------------------------------------------------
# focus_terms no-wipeout guard (#1367): a term that just restates the topic
# must not empty the exemplars; a genuine unsupported subtopic stays empty.


def _evaluation_topic_alias_mismatch() -> TopicGuidance:
    """Evaluation topic whose alias 'teacher evaluation' appears in NO exemplar
    field — mirrors the real library, where the planner restating the topic as
    focus_terms=['teacher evaluation'] would otherwise wipe the set."""
    def _ex(slug: str, district: str, subtopic: str) -> ExemplarPolicy:
        return ExemplarPolicy(
            exemplar_id=f"exemplar:{slug}",
            topic_id="evaluation",
            district=district,
            district_id=None,
            subtopic=subtopic,
            body=f"{district} runs a strong {subtopic.lower()} program.",
            source_url=f"https://teacherquality.nctq.org/contract-database/district/{slug}",  # type: ignore[arg-type]
            citation_status="ready",
        )

    return TopicGuidance(
        topic_id="evaluation",
        canonical_topic="Evaluation",
        aliases=("evaluation", "teacher evaluation", "observation"),
        canonical_url=None,
        topic_brief="Evaluation brief.",
        stances=(),
        rationales=(),
        exemplars=(
            _ex("buffalo-external-observer", "Buffalo Public Schools", "Evaluators"),
            _ex("columbus-four-observations", "Columbus City Schools", "Evaluation requirements"),
            _ex("portland-video-observations", "Portland Public Schools", "Feedback and Observations"),
        ),
    )


def test_focus_terms_restating_topic_falls_back_instead_of_emptying() -> None:
    """Planner restated the topic as focus_terms (an alias). The match is empty,
    but we keep the broad exemplar set rather than a false dead-end, warn, and
    frame the intro against the canonical topic (not the unmatched phrase)."""
    library = PolicyGuidanceLibrary.build(
        topics={"evaluation": _evaluation_topic_alias_mismatch()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["evaluation"],
        layers=["exemplars"],
        intent_summary="User asked which districts do teacher evaluation well.",
        focus_terms=["teacher evaluation"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert "couldn't find" not in manifest.body
    assert "Buffalo Public Schools" in manifest.body
    assert "Columbus City Schools" in manifest.body
    assert len(manifest.metadata["citations"]) == 3
    assert "focus_terms_unproductive:exemplars" in manifest.warnings
    # Heading uses the canonical topic, not the unmatched subtopic phrase.
    assert "for Evaluation." in manifest.body
    assert "Teacher Evaluation" not in manifest.body


def test_focus_terms_genuine_unsupported_subtopic_stays_honest_empty() -> None:
    """A real narrower subtopic the library doesn't cover ('video coaching' —
    not a topic alias) must NOT fall back to unrelated exemplars."""
    library = PolicyGuidanceLibrary.build(
        topics={"evaluation": _evaluation_topic_alias_mismatch()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["evaluation"],
        layers=["exemplars"],
        intent_summary="User asked for a video-coaching evaluation exemplar.",
        focus_terms=["video coaching"],
    )
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert "couldn't find" in manifest.body
    assert "Buffalo Public Schools" not in manifest.body
    assert manifest.metadata["citations"] == []
    assert not any(w.startswith("focus_terms_unproductive") for w in manifest.warnings)


# ---------------------------------------------------------------------------
# Advisory comparison — "should we prioritize pay or benefits?" (#1688)


def test_advisory_comparison_frames_weigh_these_priorities() -> None:
    """#1688: a weigh-two-priorities follow-up must read as a not-strictly-
    either/or decision frame (lead-in + closing), not the generic 'Here is NCTQ
    policy guidance for X and Y' that renders as two stacked, parallel dumps —
    the criterion-59 / SCENARIO_FIT failure on case 433."""
    plan = _plan(
        topic_ids=["benefits", "general-salary"],
        layers=["stances", "rationales"],
        primary_topic_id="benefits",
        response_mode="advisory_comparison",
        intent_summary="User asked whether to prioritize pay or benefits.",
    )
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    body = manifest.body
    # Weigh-them framing, not the generic parallel-dump lead-in.
    assert "either/or" in body
    assert "weigh them" in body
    assert "Here is NCTQ policy guidance for" not in body
    # Closing decision frame seals the not-a-ranking framing.
    assert "Rather than choosing one over the other" in body
    # Still grounds both topics' governed content under their headings.
    assert "## Benefits" in body
    assert "## General Salary" in body


def test_summary_mode_keeps_generic_policy_guidance_framing() -> None:
    """Guard: the advisory frame must not leak into ordinary (summary) multi-
    topic guidance — only response_mode='advisory_comparison' triggers it."""
    plan = _plan(
        topic_ids=["benefits", "general-salary"],
        layers=["stances", "rationales"],
        intent_summary="User asked for guidance on pay and benefits.",
    )
    library = _library()
    bundle = library.assemble(topic_ids=list(plan.topic_ids), layers=list(plan.layers))
    body = render_policy_guidance(plan, bundle, library=library).body

    assert "Here is NCTQ policy guidance for" in body
    assert "either/or" not in body
    assert "Rather than choosing one over the other" not in body

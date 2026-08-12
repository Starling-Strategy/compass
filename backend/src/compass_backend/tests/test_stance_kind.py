"""Tests for the StanceKind discriminator field on Stance.

Covers:
1. Inference: stance_id ending in -no-policy-stance → kind == "no_policy_stance"
2. Default: normal stance_id → kind == "policy_stance"
3. Explicit override: kind="no_policy_stance" passed directly is preserved
4. Loader integration: parse collective-bargaining.md → Stance has kind == "no_policy_stance"
5. Renderer: all-no-policy-stance topic emits dedicated leading paragraph
6. Renderer: mixed stances (one no-policy-stance + one policy-stance) falls through to bullets
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compass_backend.contracts import PolicyGuidancePlan
from compass_backend.policy_guidance.loader import parse_topic_file
from compass_backend.policy_guidance.models import (
    PolicyGuidanceLibrary,
    Stance,
    TopicGuidance,
)
from compass_backend.rendering.policy_guidance import render_policy_guidance


# ---------------------------------------------------------------------------
# 1. Inference from stance_id suffix


def test_no_policy_stance_suffix_infers_kind() -> None:
    """stance_id ending in -no-policy-stance → kind == 'no_policy_stance'."""
    s = Stance(
        stance_id="stance:collective-bargaining-no-policy-stance",
        topic_id="collective-bargaining",
        title="No NCTQ Policy Stance",
        body="NCTQ does not have a policy stance on collective bargaining.",
    )
    assert s.kind == "no_policy_stance"


# ---------------------------------------------------------------------------
# 2. Default for a normal stance


def test_normal_stance_id_defaults_to_policy_stance() -> None:
    """Regular stance_id → kind defaults to 'policy_stance'."""
    s = Stance(
        stance_id="stance:salary-living-wage",
        topic_id="general-salary",
        title="Living Wage",
        body="Districts should pay a living wage.",
    )
    assert s.kind == "policy_stance"


# ---------------------------------------------------------------------------
# 3. Explicit override is preserved


def test_explicit_no_policy_stance_kind_preserved() -> None:
    """Passing kind='no_policy_stance' explicitly on a non-suffix id is preserved."""
    s = Stance(
        stance_id="stance:foo",
        topic_id="foo-topic",
        title="Title",
        body="Body.",
        kind="no_policy_stance",
    )
    assert s.kind == "no_policy_stance"


def test_explicit_policy_stance_kind_preserved_for_suffix_id() -> None:
    """If someone explicitly passes kind='policy_stance' on a -no-policy-stance id,
    the validator overrides it to 'no_policy_stance' because the suffix is authoritative."""
    # The spec says the suffix is the deterministic discriminator — it overrides
    # an explicit 'policy_stance' on a -no-policy-stance id.
    s = Stance(
        stance_id="stance:foo-no-policy-stance",
        topic_id="foo-topic",
        title="Title",
        body="Body.",
        kind="policy_stance",  # should be overridden by the validator
    )
    assert s.kind == "no_policy_stance"


# ---------------------------------------------------------------------------
# 4. Loader integration: collective-bargaining.md


COLLECTIVE_BARGAINING_MD = Path(__file__).resolve().parents[3] / "content" / "nctq-policy" / "topics" / "collective-bargaining.md"


@pytest.mark.skipif(
    not COLLECTIVE_BARGAINING_MD.exists(),
    reason="collective-bargaining.md not present",
)
def test_loader_collective_bargaining_stance_has_no_policy_stance_kind() -> None:
    """Parsing collective-bargaining.md produces a Stance with kind == 'no_policy_stance'."""
    topic = parse_topic_file(COLLECTIVE_BARGAINING_MD)
    assert len(topic.stances) >= 1
    no_stance = next(
        (s for s in topic.stances if s.stance_id.endswith("-no-policy-stance")),
        None,
    )
    assert no_stance is not None, "expected at least one -no-policy-stance stance"
    assert no_stance.kind == "no_policy_stance"


# ---------------------------------------------------------------------------
# Helpers for renderer tests


def _no_stance_topic() -> TopicGuidance:
    """A topic where the only stance is a no-policy-stance — the collective-bargaining shape."""
    stance = Stance(
        stance_id="stance:collective-bargaining-no-policy-stance",
        topic_id="collective-bargaining",
        title="No NCTQ Policy Stance",
        body="NCTQ does not have a policy stance on collective bargaining.",
    )
    return TopicGuidance(
        topic_id="collective-bargaining",
        canonical_topic="Collective Bargaining",
        aliases=("collective bargaining",),
        canonical_url=None,
        topic_brief="Brief text.",
        stances=(stance,),
        rationales=(),
        exemplars=(),
    )


def _mixed_stances_topic() -> TopicGuidance:
    """A topic with one no-policy-stance AND one real policy_stance.

    Mixed case — should fall through to bullet rendering, not the dedicated paragraph.
    """
    no_stance = Stance(
        stance_id="stance:foo-no-policy-stance",
        topic_id="foo-topic",
        title="No NCTQ Policy Stance",
        body="NCTQ does not take a stance on foo.",
    )
    real_stance = Stance(
        stance_id="stance:foo-real-position",
        topic_id="foo-topic",
        title="Our Real Position",
        body="Districts should do X.",
    )
    return TopicGuidance(
        topic_id="foo-topic",
        canonical_topic="Foo Topic",
        aliases=("foo",),
        canonical_url=None,
        topic_brief="Foo brief.",
        stances=(no_stance, real_stance),
        rationales=(),
        exemplars=(),
    )


# ---------------------------------------------------------------------------
# 5. Renderer: all-no-policy-stance → dedicated leading paragraph


def test_renderer_all_no_policy_stance_emits_dedicated_paragraph() -> None:
    """When ALL stances for a topic are kind == 'no_policy_stance', the renderer emits
    the dedicated framing prose rather than bullet-rendering."""
    library = PolicyGuidanceLibrary.build(
        topics={"collective-bargaining": _no_stance_topic()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["collective-bargaining"],
        layers=["stances"],
        intent_summary="User asked about NCTQ's stance on collective bargaining.",
    )
    bundle = library.assemble(topic_ids=["collective-bargaining"], layers=["stances"])
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    assert "NCTQ does not take a policy stance on Collective Bargaining" in manifest.body
    # The second sentence must offer a concrete on-ramp to areas NCTQ does
    # research (an offer-framed pivot), not the older non-pivot restatement.
    assert "you can ask about areas NCTQ does research and evaluate" in manifest.body
    assert "teacher pay, evaluation, and leave" in manifest.body


def test_renderer_all_no_policy_stance_does_not_bullet_render() -> None:
    """The no-policy-stance paragraph replaces bullet rendering — no '- **…**' bullets."""
    library = PolicyGuidanceLibrary.build(
        topics={"collective-bargaining": _no_stance_topic()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["collective-bargaining"],
        layers=["stances"],
        intent_summary="User asked about NCTQ's stance on collective bargaining.",
    )
    bundle = library.assemble(topic_ids=["collective-bargaining"], layers=["stances"])
    manifest = render_policy_guidance(plan, bundle, library=library)

    # Should NOT render the stance as a bullet
    assert "- **No NCTQ Policy Stance**" not in manifest.body


def test_renderer_all_no_policy_stance_passes_validation() -> None:
    """No-policy-stance paragraph renders with no citations — validation must still pass
    (both body markers and metadata citations are zero, which is consistent)."""
    library = PolicyGuidanceLibrary.build(
        topics={"collective-bargaining": _no_stance_topic()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["collective-bargaining"],
        layers=["stances"],
        intent_summary="User asked about NCTQ's stance on collective bargaining.",
    )
    bundle = library.assemble(topic_ids=["collective-bargaining"], layers=["stances"])
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.validation_valid is True
    assert manifest.metadata["citations"] == []


# ---------------------------------------------------------------------------
# 6. Renderer: mixed stances fall through to normal bullet rendering


def test_renderer_mixed_stances_falls_through_to_bullets() -> None:
    """When topic has BOTH a no_policy_stance AND a policy_stance, render both as bullets
    (not the dedicated paragraph). The 'all' semantics are strict."""
    library = PolicyGuidanceLibrary.build(
        topics={"foo-topic": _mixed_stances_topic()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["foo-topic"],
        layers=["stances"],
        intent_summary="User asked about foo.",
    )
    bundle = library.assemble(topic_ids=["foo-topic"], layers=["stances"])
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    # Both stances should be bullet-rendered
    assert "- **No NCTQ Policy Stance**" in manifest.body
    assert "- **Our Real Position**" in manifest.body
    # The dedicated no-stance paragraph should NOT appear
    assert "NCTQ does not take a policy stance on" not in manifest.body


# ---------------------------------------------------------------------------
# 7. Renderer: no-stance topic requested via a non-stance layer (#1218)
#
# Asking for an *exemplary policy* on collective bargaining sends layers=
# ["exemplars"]. The bundle has zero exemplars (and zero stances, since the
# stances layer wasn't requested), so the renderer used to fall to the
# "couldn't find … in the current policy library" honest-empty message — which
# reads as a search miss. The topic is known but out of scope by design, so it
# must route to the typed no-policy-stance paragraph instead.


def test_renderer_exemplar_request_for_no_stance_topic_returns_no_position() -> None:
    """layers=['exemplars'] on a no-policy-stance topic returns the no-position
    paragraph, not the 'couldn't find' empty-bundle fallback (#1218)."""
    library = PolicyGuidanceLibrary.build(
        topics={"collective-bargaining": _no_stance_topic()}
    )
    plan = PolicyGuidancePlan(
        topic_ids=["collective-bargaining"],
        layers=["exemplars"],
        intent_summary="User asked for an exemplary policy on collective bargaining.",
    )
    bundle = library.assemble(
        topic_ids=["collective-bargaining"], layers=["exemplars"]
    )
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    assert (
        "NCTQ does not take a policy stance on Collective Bargaining"
        in manifest.body
    )
    # Must NOT read as a search miss / library gap.
    assert "couldn't find" not in manifest.body
    assert manifest.validation_valid is True
    assert manifest.metadata["citations"] == []


def test_renderer_real_topic_no_content_still_couldnt_find() -> None:
    """A topic that DOES take stances but has no content for the requested layer
    still returns the honest 'couldn't find' fallback — the no-position routing
    must not swallow genuine empty-layer cases."""
    real_stance = Stance(
        stance_id="stance:bar-real-position",
        topic_id="bar-topic",
        title="Our Position",
        body="Districts should do Y.",
    )
    topic = TopicGuidance(
        topic_id="bar-topic",
        canonical_topic="Bar Topic",
        aliases=("bar",),
        canonical_url=None,
        topic_brief="Bar brief.",
        stances=(real_stance,),
        rationales=(),
        exemplars=(),
    )
    library = PolicyGuidanceLibrary.build(topics={"bar-topic": topic})
    plan = PolicyGuidancePlan(
        topic_ids=["bar-topic"],
        layers=["exemplars"],
        intent_summary="User asked for an exemplary policy on bar.",
    )
    bundle = library.assemble(topic_ids=["bar-topic"], layers=["exemplars"])
    manifest = render_policy_guidance(plan, bundle, library=library)

    assert manifest.status == "rendered"
    assert "couldn't find" in manifest.body
    assert "NCTQ does not take a policy stance" not in manifest.body

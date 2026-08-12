"""W1-09 (#850) + #871: cross-module session caps have one canonical source.

Pre-W1-09 the prior-result district/metric cap was defined twice
(session/memory.py + chat.py) with value 25 — but the chat.py docstring
claimed the cap was raised to 200 in the M3 fix series. The value
disagreement silently truncated covered-universe follow-up rows. #870
moved that one cap into contracts/session.py; #871 promoted the three
remaining sibling caps (result-ref limit, memory text length, summary
text length) into the same canonical home. This module pins:

1. Each constant has one canonical source in contracts.
2. The memory.py aliases and the contracts constants are equal.
3. chat.py uses the same in-memory object, not a duplicate.
"""

from __future__ import annotations

from compass_backend import contracts
from compass_backend.contracts import ResultMemoryRef
from compass_backend import orchestration  # noqa: F401 — ensure import
from compass_backend.orchestration import result_diagnostics as result_diagnostics_module
from compass_backend.session import memory as memory_module


def test_context_result_cap_is_single_source() -> None:
    """orchestration.result_diagnostics imports _MAX_CONTEXT_RESULT_REFS from
    session.memory; the two attributes point to the same in-memory object.

    (The consumer moved from chat.py to orchestration/result_diagnostics.py in
    the #1130 decomposition; the single-source invariant is unchanged.)"""

    cap_in_memory = memory_module._MAX_CONTEXT_RESULT_REFS
    cap_in_consumer = result_diagnostics_module._MAX_CONTEXT_RESULT_REFS
    assert cap_in_consumer is cap_in_memory, (
        "W1-09: orchestration.result_diagnostics must import "
        "_MAX_CONTEXT_RESULT_REFS from session.memory rather than redefining "
        "it. Bumping the cap in memory.py must transparently affect it."
    )


def test_context_result_cap_value_supports_m3_covered_universe() -> None:
    """The cap value must accommodate the largest covered-universe selection
    M3 follow-ups round-trip — currently ~133 districts; the W1-09 plan
    pegs the value at 200 for headroom."""

    assert memory_module._MAX_CONTEXT_RESULT_REFS == 200, (
        "W1-09: cap value diverged from the M3-required 200."
    )


def test_result_memory_ref_contract_accepts_context_cap() -> None:
    """The Pydantic contract must accept the same result-ref cap memory uses."""

    districts = [
        {"district_id": district_id, "district_name": f"District {district_id}"}
        for district_id in range(memory_module._MAX_CONTEXT_RESULT_REFS)
    ]
    ref = ResultMemoryRef(
        snapshot_id="snapshot-1",
        turn_index=1,
        question="Which states allow teacher strikes?",
        result_type="metric_lookup",
        row_count=len(districts),
        districts=districts,
        digest="31 metric_lookup rows.",
    )

    assert len(ref.districts) == memory_module._MAX_CONTEXT_RESULT_REFS


def test_result_memory_ref_limit_is_single_source() -> None:
    """#871: RESULT_MEMORY_REF_LIMIT is sourced from contracts.session and
    re-exported (not redefined) by session.memory."""

    assert (
        memory_module.RESULT_MEMORY_REF_LIMIT
        is contracts.RESULT_MEMORY_REF_LIMIT
    )


def test_memory_text_max_length_is_single_source() -> None:
    """#871: the 500-char text cap on ResultMemoryRef.question / .digest /
    ConversationSummary.active_user_goal lives in contracts and is
    aliased by session.memory's _MAX_MEMORY_TEXT."""

    assert memory_module._MAX_MEMORY_TEXT == contracts.MEMORY_TEXT_MAX_LENGTH


def test_summary_text_max_length_is_single_source() -> None:
    """#871: the 1200-char cap on ConversationSummary.summary lives in
    contracts and is aliased by session.memory's _MAX_SUMMARY_TEXT."""

    assert memory_module._MAX_SUMMARY_TEXT == contracts.SUMMARY_TEXT_MAX_LENGTH

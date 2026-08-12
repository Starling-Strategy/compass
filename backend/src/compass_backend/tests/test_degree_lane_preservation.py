"""Fix 4B (#1 refusal family): planner must PRESERVE a named degree lane.

Confirmed this session with `reproduce_planner.py`: for "What is the highest
starting salary for a teacher with a BA degree?" the planner drafted
`metrics=["starting salary"]` — dropping the BA lane word — while the master's
phrasing kept it. The dropped-lane bare phrase resolves to the ambiguous
[89 bachelor's, 96 master's] bundle → adjudicator collapse → canned rescue.

Fix A (catalog/resolution.py) recovers a lane-qualified phrase by reading the
lane token OUT OF THE PHRASE (`classify_metric_lane(query)`), so the phrase must
carry the lane for the recovery to engage. This fix teaches the planner to keep
the lane word in `MetricSpec.name` (and mirror it onto `sort.field` /
`FilterSpec.field`) whenever the user names a lane — symmetrically for BA and MA.

These assertions are gateway-free (snippet selection + loaded instruction text).
The end-to-end proof that BA→89 and MA→96 through the full stack is behavioral
and lives in the PR notes (the in-process planner draft short-circuits the
catalog finalizer and cannot prove resolution — see reproduce_planner.py scope
limit).

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_degree_lane_preservation.py --tb=short
"""

from __future__ import annotations

from types import SimpleNamespace

from compass_backend.instructions.loader import load_model_instructions
from compass_backend.planning.instruction_snippets import (
    TEACHER_COMPENSATION_SALARY,
    select_planner_instruction_snippets,
)

_BA_PROMPT = "What is the highest starting salary for a teacher with a BA degree?"
_MA_PROMPT = "What is the highest starting salary for a teacher with a master's degree?"


def _names(message: str) -> list[str]:
    deps = SimpleNamespace(message=message, query_context=None, recent_routes=())
    return [s.name for s in select_planner_instruction_snippets(deps)]


# ─── The lane-preservation snippet must actually reach the planner ────────────


def test_compensation_snippet_fires_on_ba_prompt() -> None:
    # If the snippet does not fire on the reporter's literal prompt, the
    # reinforcement rule is inert — the always-on base rule below must carry it.
    assert TEACHER_COMPENSATION_SALARY.name in _names(_BA_PROMPT)


def test_compensation_snippet_fires_on_ma_prompt() -> None:
    assert TEACHER_COMPENSATION_SALARY.name in _names(_MA_PROMPT)


# ─── Always-on base rule (planner.md) carries the load-bearing instruction ────


def test_base_planner_md_teaches_lane_preservation() -> None:
    body = load_model_instructions("planner.md").lower()
    # The base rule must exist regardless of whether a snippet is selected.
    assert "salary degree lanes" in body
    # It must instruct KEEPING the lane word in the metric name, not dropping it.
    assert "keep the lane word" in body
    assert 'bare `"starting salary"`' in body
    # And mirroring the lane-bearing phrase onto sort/filter fields.
    assert "sort.field" in body


# ─── Snippet body reinforces the same rule, symmetric BA/MA ───────────────────


def test_snippet_body_teaches_lane_preservation_symmetric() -> None:
    body = TEACHER_COMPENSATION_SALARY.body.lower()
    assert "keep the lane word" in body
    # Explicitly names the BA-drop trap this fix closes.
    assert "do not drop the ba lane" in body
    # A worked BA ranking example that keeps "ba" on both name and sort.field.
    assert "ba starting salary" in body

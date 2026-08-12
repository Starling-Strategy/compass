"""SelectionSpec must reject mixed scope/field combinations.

Regression for #1069. The Turn-3 planner of session
``23a23b7e-bc38-4ba1-ba23-2d0fa88894be`` emitted
``scope=state, states=["VT"], districts=["Dallas ISD"]`` to compare Dallas
against Vermont. The validator accepted it because ``state`` scope only
required a non-empty ``states`` list, with no check on ``districts``. The
executor enumerates VT districts only, the Dallas anchor is silently
dropped, and the stylist hallucinates "no reviewed data for Dallas ISD"
even though the prior turn already reported its $62,000 BA salary.

These tests pin the only valid SelectionSpec shapes per scope so the
Pydantic AI planner's output-validation retry catches the bad encoding
and re-emits a shape the executor honors.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.contracts.planning import SelectionSpec


def test_state_scope_rejects_named_districts():
    with pytest.raises(ValidationError):
        SelectionSpec(scope="state", states=["VT"], districts=["Dallas ISD"])


def test_all_covered_districts_rejects_named_districts():
    with pytest.raises(ValidationError):
        SelectionSpec(scope="all_covered_districts", districts=["Dallas ISD"])


def test_all_covered_districts_accepts_states_as_filter():
    """``states`` narrows ``all_covered_districts`` to that state's universe."""

    spec = SelectionSpec(scope="all_covered_districts", states=["TX"])
    assert spec.states == ["TX"]


def test_largest_districts_rejects_named_districts():
    with pytest.raises(ValidationError):
        SelectionSpec(scope="largest_districts", districts=["Dallas ISD"])


def test_largest_districts_accepts_states_as_filter():
    """``states`` narrows ``largest_districts`` to that state's largest."""

    spec = SelectionSpec(scope="largest_districts", states=["CA"])
    assert spec.states == ["CA"]


def test_named_districts_accepts_states_as_narrowing_filter():
    """``states`` on ``named_districts`` is read by the executor as a
    narrowing filter over the named districts (see ``execution/scoping``
    and ``quality/validators/selection``). Keep this shape legal."""

    spec = SelectionSpec(
        scope="named_districts",
        districts=["Dallas ISD", "Burlington School District"],
        states=["VT"],
    )
    assert spec.scope == "named_districts"
    assert spec.states == ["VT"]


def test_state_scope_accepts_states_only():
    spec = SelectionSpec(scope="state", states=["VT"])
    assert spec.scope == "state"
    assert spec.states == ["VT"]
    assert spec.districts == []


def test_named_districts_accepts_anchor_plus_comparison_group():
    spec = SelectionSpec(
        scope="named_districts",
        districts=["Dallas ISD", "Burlington School District", "Champlain Valley"],
    )
    assert spec.scope == "named_districts"
    assert spec.districts == [
        "Dallas ISD",
        "Burlington School District",
        "Champlain Valley",
    ]
    assert spec.states == []


def test_all_covered_districts_accepts_bare():
    spec = SelectionSpec(scope="all_covered_districts")
    assert spec.scope == "all_covered_districts"
    assert spec.districts == []
    assert spec.states == []


def test_unspecified_scope_allows_empty_fields():
    spec = SelectionSpec(scope="unspecified")
    assert spec.scope == "unspecified"
    assert spec.districts == []
    assert spec.states == []

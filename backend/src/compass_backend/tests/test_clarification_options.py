"""Contract tests for structured clarification options (#1348).

A clarification turn (e.g. district disambiguation) carries a small list of
grounded, clickable answer choices alongside its prose question. Each option is
born from a real catalog candidate — never invented — so a click can resume the
query deterministically. Free text always stays first-class; these options are
additive and default-empty so every existing clarify turn is unaffected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.contracts import (
    ChatRequest,
    ClarificationOption,
    ClarificationRequest,
    ConversationMemory,
)


class TestClarificationOption:
    def test_minimal_option_has_value_and_label(self) -> None:
        option = ClarificationOption(value="2700", label="Cleveland County Schools, NC")
        assert option.value == "2700"
        assert option.label == "Cleveland County Schools, NC"
        assert option.detail is None

    def test_optional_detail_secondary_line(self) -> None:
        option = ClarificationOption(
            value="2700",
            label="Cleveland County Schools, NC",
            detail="North Carolina · ~15,000 students",
        )
        assert option.detail == "North Carolina · ~15,000 students"

    def test_value_and_label_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            ClarificationOption(value="", label="x")
        with pytest.raises(ValidationError):
            ClarificationOption(value="x", label="")

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ClarificationOption(value="1", label="x", district_id=1)  # type: ignore[call-arg]


class TestClarificationRequestCandidateOptions:
    def test_candidate_options_default_empty(self) -> None:
        """Every existing clarify turn keeps working — options are additive."""
        request = ClarificationRequest(
            question="Which district do you mean?",
            missing_fields=["district"],
        )
        assert request.candidate_options == []

    def test_carries_grounded_options(self) -> None:
        request = ClarificationRequest(
            question="Which district do you mean?",
            missing_fields=["district"],
            candidate_options=[
                ClarificationOption(value="2700", label="Cleveland County Schools, NC"),
                ClarificationOption(value="4011", label="Cleveland County, OK"),
            ],
        )
        assert [o.value for o in request.candidate_options] == ["2700", "4011"]

    def test_prose_candidates_and_structured_options_coexist(self) -> None:
        """The prose `candidates` list stays untouched (load-bearing elsewhere)."""
        request = ClarificationRequest(
            question="Which district do you mean?",
            missing_fields=["district"],
            candidates=["Cleveland County Schools, NC", "Cleveland County, OK"],
            candidate_options=[
                ClarificationOption(value="2700", label="Cleveland County Schools, NC"),
            ],
        )
        assert request.candidates == [
            "Cleveland County Schools, NC",
            "Cleveland County, OK",
        ]
        assert request.candidate_options[0].value == "2700"


class TestChatRequestSelectedOption:
    def test_selected_option_defaults_none(self) -> None:
        request = ChatRequest(message="hello")
        assert request.selected_option is None

    def test_selected_option_carries_machine_handle(self) -> None:
        request = ChatRequest(message="Cleveland County Schools, NC", selected_option="2700")
        assert request.selected_option == "2700"

    def test_selected_option_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            ChatRequest(message="x", selected_option="")


class TestDistrictClarificationOption:
    """The grounded mapper: a covered DistrictCandidate -> ClarificationOption."""

    def test_maps_id_label_and_detail(self) -> None:
        from compass_backend.catalog import DistrictCandidate
        from compass_backend.execution.selection import _district_clarification_option

        option = _district_clarification_option(
            DistrictCandidate(
                district_id=2700,
                district_name="Cleveland County Schools",
                state="NC",
                city="Shelby",
                enrollment=15000,
            )
        )
        assert option.value == "2700"
        assert option.label == "Cleveland County Schools, NC"
        assert option.detail == "15,000 students · Shelby"

    def test_detail_none_when_no_enrollment_or_city(self) -> None:
        from compass_backend.catalog import DistrictCandidate
        from compass_backend.execution.selection import _district_clarification_option

        option = _district_clarification_option(
            DistrictCandidate(district_id=99, district_name="Lone District", state="AR")
        )
        assert option.value == "99"
        assert option.label == "Lone District, AR"
        assert option.detail is None


class TestMetricClarificationOption:
    """The grounded mapper: a governed MetricCandidate -> ClarificationOption.

    The metric analogue of the district mapper (#1348). ``value`` is the
    canonical, re-resolvable metric name (a click sends it back and execution
    re-grounds it through the catalog); ``detail`` is the catalog ``topic``,
    which is the only secondary line ``MetricCandidate`` carries (no definition
    text exists on it).
    """

    def test_maps_name_and_topic(self) -> None:
        from compass_backend.catalog import MetricCandidate
        from compass_backend._clarify_helpers import _metric_clarification_option

        option = _metric_clarification_option(
            MetricCandidate(
                metric_id=57,
                name="Minimum amount of elementary teacher planning time per week (in minutes)",
                topic="Planning time",
            )
        )
        assert option.value == (
            "Minimum amount of elementary teacher planning time per week (in minutes)"
        )
        assert option.label == option.value
        assert option.detail == "Planning time"

    def test_detail_none_when_no_topic(self) -> None:
        from compass_backend.catalog import MetricCandidate
        from compass_backend._clarify_helpers import _metric_clarification_option

        option = _metric_clarification_option(
            MetricCandidate(metric_id=12, name="Teacher workdays")
        )
        assert option.value == "Teacher workdays"
        assert option.detail is None

    def test_metric_clarification_populates_structured_options(self) -> None:
        """The execution-origin clarify builder now carries clickable options.

        Closes the metric half of #1348's generation gap: the prose
        ``candidates`` and the structured ``candidate_options`` are both built
        from the in-hand candidate list, deduped by ``metric_id``.
        """
        from compass_backend.catalog import MetricCandidate
        from compass_backend._clarify_helpers import _metric_clarification

        clarification = _metric_clarification(
            "planning time",
            operation="rank",
            candidates=[
                MetricCandidate(metric_id=57, name="Elementary planning time", topic="Planning time"),
                MetricCandidate(metric_id=58, name="Middle planning time", topic="Planning time"),
                # Duplicate metric_id -> deduped out of both surfaces.
                MetricCandidate(metric_id=57, name="Elementary planning time", topic="Planning time"),
            ],
        )
        assert clarification.candidates == ["Elementary planning time", "Middle planning time"]
        assert [o.value for o in clarification.candidate_options] == [
            "Elementary planning time",
            "Middle planning time",
        ]
        assert clarification.missing_fields == ["metric"]


class TestResolveSelectionPopulatesOptions:
    """The execution-path clarify (case 340) carries grounded options end-to-end."""

    def test_ambiguous_selection_yields_grounded_options(self) -> None:
        import asyncio

        from compass_backend.catalog import DistrictCandidate, DistrictResolution
        from compass_backend.contracts.planning import QueryPlan, SelectionSpec
        from compass_backend.execution.selection import resolve_selection
        from compass_backend.execution.types import ExecutionClarification

        class _FakeCatalog:
            async def resolve_districts(self, names, *, states=None):  # noqa: ANN001
                return DistrictResolution(
                    ambiguous={
                        "Cleveland County": [
                            DistrictCandidate(
                                district_id=2700,
                                district_name="Cleveland County Schools",
                                state="NC",
                                city="Shelby",
                                enrollment=15000,
                            ),
                            DistrictCandidate(
                                district_id=4011,
                                district_name="Cleveland County",
                                state="OK",
                                city="Norman",
                                enrollment=9100,
                            ),
                        ]
                    }
                )

        plan = QueryPlan(
            inherit_from_session=True,
            operation="rank",
            question="What are evaluation policies in Cleveland County?",
            selection=SelectionSpec(scope="named_districts", districts=["Cleveland County"]),
        )
        outcome = asyncio.run(
            resolve_selection(plan, _FakeCatalog(), academic_year="2024-2025")
        )

        assert isinstance(outcome, ExecutionClarification)
        options = outcome.clarification.candidate_options
        assert [o.value for o in options] == ["2700", "4011"]
        assert options[0].label == "Cleveland County Schools, NC"
        assert options[0].detail == "15,000 students · Shelby"
        # The prose `candidates` list is unchanged (still populated).
        assert outcome.clarification.candidates == [
            "Cleveland County Schools, NC",
            "Cleveland County, OK",
        ]


class TestConversationMemoryPendingOptions:
    def test_pending_clarification_options_default_empty(self) -> None:
        memory = ConversationMemory()
        assert memory.pending_clarification_options == []

    def test_pending_options_round_trip(self) -> None:
        """Offered options persist so the next turn can validate a clicked id."""
        memory = ConversationMemory(
            pending_clarification_options=[
                ClarificationOption(value="2700", label="Cleveland County Schools, NC"),
                ClarificationOption(value="4011", label="Cleveland County, OK"),
            ]
        )
        restored = ConversationMemory.model_validate(memory.model_dump())
        assert [o.value for o in restored.pending_clarification_options] == [
            "2700",
            "4011",
        ]

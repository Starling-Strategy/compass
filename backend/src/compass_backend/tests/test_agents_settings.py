"""Agent settings/export regressions for the fresh Compass runtime."""

from __future__ import annotations

import compass_backend.agents as agents
from compass_backend.agents import (
    AgentProfile,
    model_settings_for_profile,
)


def test_agents_package_exports_planner_settings_surface() -> None:
    """The agent package should expose only active model-call settings."""

    assert agents.agent_model_settings is not None
    assert agents.AgentProfile is AgentProfile
    assert agents.model_settings_for_profile is model_settings_for_profile
    assert not hasattr(agents, "model_settings_for_writer")
    assert not hasattr(agents, "run_" + "plain" + "_chat")
    assert not hasattr(agents, "create_" + "plain" + "_chat_agent")


def test_agent_profiles_only_include_active_agents() -> None:
    """Retired generator, critic, and LLM writer profiles should not linger.

    Updated in B-spine PR 5 to include 'criterion_classifier' (Safeguard 4
    of the 4-safeguard criterion selector).
    """

    assert {profile.value for profile in AgentProfile} == {
        "answer_stylist",
        "clarify_stylist",
        "catalog_adjudicator",
        "criterion_classifier",
        "judge",
        "planner",
    }


def test_planner_profile_settings_remain_available() -> None:
    """Planner settings continue to resolve after retiring plain chat."""

    settings = model_settings_for_profile(AgentProfile.PLANNER)

    assert settings["max_tokens"] == agents.agent_model_settings.planner_max_tokens
    assert settings["temperature"] == agents.agent_model_settings.default_temperature


def test_answer_stylist_profile_settings_remain_available() -> None:
    """Answer-layer rewriting gets its own high-capability model profile."""

    loaded = agents.AgentModelSettings(_env_file=None)
    settings = model_settings_for_profile(AgentProfile.ANSWER_STYLIST, source=loaded)

    assert loaded.answer_stylist_model == "gateway/anthropic:claude-opus-4-6"
    assert loaded.answer_stylist_max_tokens == 2048
    assert settings["max_tokens"] == loaded.answer_stylist_max_tokens
    assert settings["temperature"] == loaded.default_temperature
    assert settings["timeout"] == loaded.default_timeout_seconds


def test_answer_stylist_settings_read_compass_agent_env(monkeypatch) -> None:
    """Answer-stylist model and token budget are env-backed like other agents."""

    monkeypatch.setenv("COMPASS_AGENT_ANSWER_STYLIST_MODEL", "test-answer-model")
    monkeypatch.setenv("COMPASS_AGENT_ANSWER_STYLIST_MAX_TOKENS", "1234")

    loaded = agents.AgentModelSettings(_env_file=None)
    settings = model_settings_for_profile(AgentProfile.ANSWER_STYLIST, source=loaded)

    assert loaded.answer_stylist_model == "test-answer-model"
    assert loaded.answer_stylist_max_tokens == 1234
    assert settings["max_tokens"] == 1234


def test_planner_thinking_policy_defaults_to_off() -> None:
    """Planner thinking is an explicit planner-only feature flag."""

    loaded = agents.AgentModelSettings(_env_file=None)

    assert loaded.planner_thinking_policy == "off"
    assert loaded.planner_thinking_max_effort == "medium"


def test_planner_thinking_policy_reads_compass_agent_env(
    monkeypatch,
) -> None:
    """The SSN-271 flag lives under the agent settings prefix."""

    monkeypatch.setenv("COMPASS_AGENT_PLANNER_THINKING_POLICY", "selected")
    monkeypatch.setenv("COMPASS_AGENT_PLANNER_THINKING_MAX_EFFORT", "high")

    loaded = agents.AgentModelSettings(_env_file=None)

    assert loaded.planner_thinking_policy == "selected"
    assert loaded.planner_thinking_max_effort == "high"


def test_catalog_adjudicator_profile_settings_remain_available() -> None:
    """Catalog adjudication gets its own constrained model-call profile."""

    loaded = agents.AgentModelSettings(_env_file=None)
    settings = model_settings_for_profile(
        AgentProfile.CATALOG_ADJUDICATOR, source=loaded
    )

    # Assert the exact model string, not mere truthiness: a wrong/typo'd model
    # string would still be truthy but would route adjudication to the wrong model.
    assert loaded.catalog_adjudicator_model == "gateway/anthropic:claude-haiku-4-5"
    assert settings["max_tokens"] == loaded.catalog_adjudicator_max_tokens
    assert settings["temperature"] == loaded.default_temperature


def test_judge_profile_settings_remain_available() -> None:
    """Runtime verdict judges use the same constrained settings surface."""

    loaded = agents.AgentModelSettings(_env_file=None)
    settings = model_settings_for_profile(AgentProfile.JUDGE, source=loaded)

    # Assert the exact model string, not mere truthiness: a wrong/typo'd model
    # string would still be truthy but would grade verdicts on the wrong model.
    assert loaded.judge_model == "gateway/anthropic:claude-haiku-4-5"
    assert settings["max_tokens"] == loaded.judge_max_tokens
    assert settings["temperature"] == loaded.default_temperature
    assert settings["timeout"] == loaded.default_timeout_seconds


def test_judge_load_control_defaults_are_conservative() -> None:
    """Background judge fan-out has explicit usage and queue controls."""

    s = agents.agent_model_settings

    assert s.judge_request_limit == 2
    assert s.judge_max_concurrency == 4
    assert s.judge_max_queue == 64


def test_clarify_stylist_profile_uses_focused_max_tokens():
    settings = model_settings_for_profile(AgentProfile.CLARIFY_STYLIST)
    assert settings["max_tokens"] == 1024


def test_clarify_stylist_model_defaults_to_answer_stylist_model_string():
    # Same model as answer stylist by default; can be overridden via
    # COMPASS_AGENT_CLARIFY_STYLIST_MODEL env var.
    assert (
        agents.agent_model_settings.clarify_stylist_model
        == agents.agent_model_settings.answer_stylist_model
    )

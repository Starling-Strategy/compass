"""Tests for the Compass Operations cost/utilization dashboard."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fasthtml.common import to_xml

from nctqai.routes.compass.operations import _api_key_section, _costs_section
from nctqai.services import compass_stats
from nctqai.services.compass_stats import ApiKeyUsageSummary, CostStats


def test_cost_stats_filter_to_chat_usage_and_compute_plain_language_rollups(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def fake_run_sql(sql: str, binds: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        calls.append((sql, binds))
        return [
            {
                "total_cost": 1.2,
                "input_tokens": 1_000,
                "output_tokens": 500,
                "cache_read_tokens": 4_000,
                "cache_write_tokens": 500,
                "requests": 9,
                "sessions": 3,
                "turns": 6,
                "cache_savings": 0.75,
                "unpriced_rows": 2,
                "unpriced_tokens": 300,
            }
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)

    since = datetime(2026, 7, 1, tzinfo=UTC)
    result = compass_stats.get_cost_stats(since, api_key_id="pa_dev_c36eed70")

    sql, binds = calls[0]
    assert "u.source = 'chat'" in sql
    assert "u.api_key_id = %s" in sql
    assert "JOIN usage_sessions" not in sql
    assert binds == (since.replace(tzinfo=None), "pa_dev_c36eed70")
    assert result.total_cost == 1.2
    assert result.cost_per_session == 0.4
    assert result.cost_per_turn == 0.2
    assert result.avg_tokens_per_session == 2_000
    assert result.avg_tokens_per_turn == 1_000
    assert result.avg_rounds_per_session == 2.0
    assert result.cache_hit_rate == 72.7
    assert result.cache_savings == 0.75
    assert result.unpriced_rows == 2
    assert result.unpriced_token_share == 5.0


def test_cost_breakdown_queries_exclude_eval_rows_and_other_keys(monkeypatch) -> None:
    captured: list[tuple[str, tuple[Any, ...]]] = []

    def fake_run_sql(sql: str, binds: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        captured.append((sql, binds))
        return []

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)

    compass_stats.get_cost_by_phase(api_key_id="pa_dev_c36eed70")
    compass_stats.get_cost_by_model(api_key_id="pa_dev_c36eed70")
    compass_stats.get_cost_trend(api_key_id="pa_dev_c36eed70")
    compass_stats.get_observed_model_config(api_key_id="pa_dev_c36eed70")

    assert captured
    assert all("u.source = 'chat'" in sql for sql, _ in captured)
    assert all("u.api_key_id = %s" in sql for sql, _ in captured)
    assert all(binds == ("pa_dev_c36eed70",) for _, binds in captured)
    assert captured[0][0].count("NULLS LAST") == 1


def test_recent_api_keys_returns_safe_key_ids(monkeypatch) -> None:
    captured: list[tuple[str, tuple[Any, ...]]] = []

    def fake_run_sql(sql: str, binds: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        captured.append((sql, binds))
        return [
            {
                "key_id": "pa_dev_c36eed70",
                "name": "compass-admin",
                "owner_email": "macon@starlingstrategy.com",
                "last_used_at": datetime(2026, 7, 7, 14, 0, tzinfo=UTC),
                "request_count": 12,
            }
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)

    result = compass_stats.get_recent_api_keys(key_id="pa_dev_c36eed70")

    sql, binds = captured[0]
    assert "FROM compass.api_keys" in sql
    assert "revoked_at IS NULL" in sql
    assert "key_id = %s" in sql
    assert binds == ("pa_dev_c36eed70", 5)
    assert result == [
        ApiKeyUsageSummary(
            key_id="pa_dev_c36eed70",
            name="compass-admin",
            owner_email="macon@starlingstrategy.com",
            last_used_at=datetime(2026, 7, 7, 14, 0, tzinfo=UTC),
            request_count=12,
        )
    ]


def test_costs_section_leads_with_budget_context_and_key_id() -> None:
    cost = CostStats(
        total_cost=6.25,
        cost_per_session=1.25,
        cost_per_turn=0.2,
        avg_tokens_per_session=10_000,
        avg_tokens_per_turn=5_000,
        avg_rounds_per_session=2.0,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=40,
        cache_write_tokens=10,
        requests=3,
        sessions=5,
        turns=10,
        cache_hit_rate=25.0,
        cache_savings=1.5,
        unpriced_rows=0,
        unpriced_tokens=0,
        unpriced_token_share=0.0,
    )
    keys = [
        ApiKeyUsageSummary(
            key_id="pa_dev_c36eed70",
            name="compass-admin",
            owner_email="macon@starlingstrategy.com",
            last_used_at=datetime(2026, 7, 7, 14, 0, tzinfo=UTC),
            request_count=12,
        )
    ]

    html = to_xml(_costs_section(cost, keys, api_key_id="pa_dev_c36eed70"))

    assert "Production costs using API Key id pa_dev_c36eed70" in html
    assert "Measured AI cost" in html
    assert "API key id" in html
    assert "Planning AI budget" in html
    assert "$825.00/mo" in html
    assert "$0.17 per turn" in html
    assert "Fixed platform baseline" in html
    assert "pa_dev_c36eed70" in html
    assert "This report is scoped to API Key id pa_dev_c36eed70." in html
    assert "All chat usage rows are priced." in html


def test_api_key_section_explains_configured_key_scope() -> None:
    html = to_xml(
        _api_key_section(
            [
                ApiKeyUsageSummary(
                    key_id="pa_dev_c36eed70",
                    name="compass-admin",
                    owner_email="macon@starlingstrategy.com",
                    last_used_at=datetime(2026, 7, 7, 14, 0, tzinfo=UTC),
                    request_count=12,
                )
            ]
        )
    )

    assert "API keys" in html
    assert "pa_dev_c36eed70" in html
    assert "report is filtered to the configured key id" in html

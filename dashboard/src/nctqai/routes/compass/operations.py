"""Compass Operations — Starling-only system metrics.

Cost, token, and (when populated) latency stats. Pulled out of the funder-facing
Overview so reporting stays uncluttered. Same range pill behavior.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from fasthtml.common import Div, H2, H3, P, Span
from starlette.requests import Request

from nctqai.components import KpiCard, KpiRow
from nctqai.components.compass.bars import bar_row
from nctqai.components.compass.range_pills import range_selector, resolve_since
from nctqai.config import Config
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import require_compass_admin, run_in_thread, safe
from nctqai.services.compass_stats import (
    ApiKeyUsageSummary,
    CostStats,
    LatencyStats,
    get_cost_by_model,
    get_cost_by_phase,
    get_cost_stats,
    get_cost_trend,
    get_latency_stats,
    get_observed_model_config,
    get_recent_api_keys,
)

logger = logging.getLogger(__name__)

_MONTHLY_FIXED_INFRASTRUCTURE = 468.50
_MONTHLY_AI_BUDGET = 825.00
_MONTHLY_BUDGET_USERS = 250
_MONTHLY_BUDGET_SESSIONS_PER_USER = 4
_MONTHLY_BUDGET_TURNS_PER_SESSION = 5
_BUDGET_TURNS_PER_MONTH = (
    _MONTHLY_BUDGET_USERS
    * _MONTHLY_BUDGET_SESSIONS_PER_USER
    * _MONTHLY_BUDGET_TURNS_PER_SESSION
)
_BUDGET_COST_PER_TURN = _MONTHLY_AI_BUDGET / _BUDGET_TURNS_PER_MONTH


def _format_cost(usd: float) -> str:
    if usd <= 0:
        return "$0"
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1:
        return f"${usd:.2f}"
    return f"${usd:,.2f}"


def _format_tokens(n: int) -> str:
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def _format_ms(ms: int) -> str:
    if not ms:
        return "—"
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.1f}s"


def _format_percent(value: float) -> str:
    if value <= 0:
        return "0%"
    return f"{value:.1f}%"


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "never used"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%b %-d, %Y %-I:%M %p UTC")


def _stat(label: str, value: str) -> Span:
    return Span(f"{label}: ", Span(value, cls="ops-stat-value"), cls="ops-stat")


def _section_heading(title: str, subtitle: str = "") -> Div:
    parts = [H2(title, cls="overview-section-title")]
    if subtitle:
        parts.append(P(subtitle, cls="overview-section-subtitle"))
    return Div(*parts, cls="overview-section-heading")


def _api_key_label(
    api_key_id: str | None,
    keys: list[ApiKeyUsageSummary],
) -> tuple[str, str]:
    if not api_key_id:
        return "not configured", "set NCTQAI_COMPASS_API_KEY"
    if not keys:
        return api_key_id, "key id configured; no matching auth row found"
    key = keys[0]
    subtitle = f"{key.name or key.owner_email} · {_format_datetime(key.last_used_at)}"
    return key.key_id, subtitle


def _projected_monthly_ai_cost(cost: CostStats) -> float:
    if cost.cost_per_turn <= 0:
        return 0
    return cost.cost_per_turn * _BUDGET_TURNS_PER_MONTH


def _cost_explanation(cost: CostStats, api_key_id: str | None) -> Div:
    projected = _projected_monthly_ai_cost(cost)
    comparison = (
        "No chat turns in this range yet."
        if cost.turns == 0
        else (
            f"At this range's observed {_format_cost(cost.cost_per_turn)} "
            f"question cost, the 5,000-turn planning model would be about "
            f"{_format_cost(projected)} per month."
        )
    )
    trust = (
        "All chat usage rows are priced."
        if cost.unpriced_rows == 0
        else (
            f"{cost.unpriced_rows:,} usage rows are unpriced, covering "
            f"{_format_percent(cost.unpriced_token_share)} of tokens; shown "
            "costs are a lower bound."
        )
    )
    return Div(
        P(
            f"This report is scoped to API Key id {api_key_id}." if api_key_id
            else "This report is not scoped because no Compass API key id is configured.",
            cls="overview-section-subtitle",
        ),
        P(
            "It measures variable AI cost for live Compass chat and excludes "
            "eval sweeps, legacy rows, and chat rows written by other keys. "
            "Fixed Azure hosting is shown as a planning baseline because it "
            "comes from Azure billing, not the chat ledger.",
            cls="overview-section-subtitle",
        ),
        Div(
            _stat("Planning AI budget", _format_cost(_MONTHLY_AI_BUDGET) + "/mo"),
            _stat(
                "Budget assumption",
                f"{_format_cost(_BUDGET_COST_PER_TURN)} per turn",
            ),
            _stat(
                "Fixed platform baseline",
                _format_cost(_MONTHLY_FIXED_INFRASTRUCTURE) + "/mo",
            ),
            _stat("Trust check", trust),
            cls="ops-stat-line ops-stat-line--stacked",
        ),
        P(comparison, cls="overview-section-subtitle"),
        cls="overview-panel ops-cost-note",
    )


def _costs_section(
    cost: CostStats,
    api_keys: list[ApiKeyUsageSummary],
    *,
    api_key_id: str | None,
) -> Div:
    key_value, key_subtitle = _api_key_label(api_key_id, api_keys)
    return Div(
        _section_heading(
            f"Production costs using API Key id {api_key_id}"
            if api_key_id
            else "Production costs",
            "Measured AI spend for the configured Compass API key, with budget context from the Compass cost model.",
        ),
        KpiRow(
            KpiCard(_format_cost(cost.total_cost), "Measured AI cost", subtitle="selected range"),
            KpiCard(
                _format_cost(cost.cost_per_turn),
                "Question cost",
                subtitle=f"{cost.turns:,} user questions",
            ),
            KpiCard(
                _format_cost(cost.cost_per_session),
                "Conversation cost",
                subtitle=f"{cost.sessions:,} sessions",
            ),
            KpiCard(
                _format_cost(cost.cache_savings),
                "Caching saved",
                subtitle=f"{_format_percent(cost.cache_hit_rate)} cache hit rate",
            ),
            KpiCard(key_value, "API key id", subtitle=key_subtitle),
            cls="kpi-cards--grid4",
        ),
        _cost_explanation(cost, api_key_id),
        cls="overview-section",
    )


def _usage_section(cost: CostStats) -> Div:
    unpriced = (
        f"{cost.unpriced_rows:,} rows · {_format_percent(cost.unpriced_token_share)} of tokens"
        if cost.unpriced_rows
        else "all priced"
    )
    return Div(
        _section_heading(
            "Usage details",
            "Token volume explains why a question costs what it costs.",
        ),
        KpiRow(
            KpiCard(
                _format_tokens(cost.avg_tokens_per_session),
                "Avg tokens",
                subtitle="per conversation",
            ),
            KpiCard(
                _format_tokens(cost.avg_tokens_per_turn),
                "Avg tokens",
                subtitle="per user question",
            ),
            KpiCard(
                f"{cost.avg_rounds_per_session:.1f}",
                "Avg rounds",
                subtitle="user questions/session",
            ),
            KpiCard(unpriced, "Pricing status", subtitle="costs are lower bound if nonzero"),
        ),
        Div(
            _stat("Fresh input", _format_tokens(cost.input_tokens)),
            _stat("Cache read", _format_tokens(cost.cache_read_tokens)),
            _stat("Cache write", _format_tokens(cost.cache_write_tokens)),
            _stat("Output", _format_tokens(cost.output_tokens)),
            cls="ops-stat-line",
        ),
        cls="overview-section",
    )


def _observed_model_section(models: list[dict]) -> Div:
    if not models:
        body = P("No current chat usage rows yet.", cls="text-muted text-sm")
    else:
        body = Div(
            *[
                Div(
                    Span(str(row.get("phase") or "unknown"), cls="bar-label"),
                    Span(
                        str(
                            row.get("configured_model")
                            or row.get("model_actual")
                            or "unknown"
                        ),
                        cls="ops-stat-value",
                    ),
                    cls="ops-stat-line",
                )
                for row in models
            ],
            cls="overview-panel",
        )
    return Div(
        _section_heading(
            "Observed model configuration",
            "Latest chat row per Compass step. New turns write configured and actual model refs.",
        ),
        body,
        cls="overview-section",
    )


def _api_key_section(keys: list[ApiKeyUsageSummary]) -> Div:
    if not keys:
        body = P("No API key usage recorded yet.", cls="text-muted text-sm")
    else:
        body = Div(
            *[
                Div(
                    Span(key.key_id, cls="bar-label"),
                    Span(
                        f"{key.name or key.owner_email} · "
                        f"{key.request_count:,} requests · "
                        f"{_format_datetime(key.last_used_at)}",
                        cls="ops-stat-value",
                    ),
                    cls="ops-stat-line",
                )
                for key in keys
            ],
            cls="overview-panel",
        )
    return Div(
        _section_heading(
            "API keys",
            "The report is filtered to the configured key id. Request counts come from the auth table.",
        ),
        body,
        cls="overview-section",
    )


def _bar_section(title: str, rows: list[dict], label_key: str) -> Div:
    if not rows:
        body = P("No data yet.", cls="text-muted text-sm")
    else:
        total = sum(float(p.get("cost", 0) or 0) for p in rows)
        total_units = int(total * 10_000) if total else 1
        body = Div(
            *[
                bar_row(
                    str(p.get(label_key) or "unknown"),
                    int(float(p.get("cost", 0) or 0) * 10_000),
                    total_units,
                    value_label=_format_cost(float(p.get("cost", 0) or 0)),
                )
                for p in rows
            ],
            cls="bar-chart",
        )
    return Div(
        H3(title, cls="overview-subsection-title"),
        body,
        cls="overview-panel",
    )


def _trend_section(points: list[dict]) -> Div:
    if not points:
        body = P("No data yet.", cls="text-muted text-sm")
    else:
        max_cost = max((p["cost"] for p in points), default=0) or 1
        body = Div(
            *[
                bar_row(
                    str(p["label"]),
                    int(float(p["cost"]) * 10_000),
                    int(max_cost * 10_000),
                    value_label=_format_cost(float(p["cost"])),
                )
                for p in points
            ],
            cls="bar-chart",
        )
    return Div(
        H3("Daily cost trend", cls="overview-subsection-title"),
        body,
        cls="overview-panel",
    )


def _breakdown_section(
    trend: list[dict],
    cost_by_phase: list[dict],
    cost_by_model: list[dict],
) -> Div:
    return Div(
        _section_heading("Where each dollar goes"),
        Div(
            _trend_section(trend),
            _bar_section("By Compass step", cost_by_phase, "phase"),
            _bar_section("By model", cost_by_model, "model"),
            cls="overview-grid-2",
        ),
        cls="overview-section",
    )


def _latency_section(latency: LatencyStats) -> Div:
    if latency.avg_total_ms == 0:
        return Div(
            H3("Latency", cls="overview-subsection-title"),
            P(
                "Latency columns are not currently populated by the v2 pipeline. "
                "Real values will return when the duration writes are restored.",
                cls="text-muted text-sm",
            ),
            cls="overview-panel overview-section",
        )
    summary = Div(
        _stat("Avg total", _format_ms(latency.avg_total_ms)),
        _stat("Avg generator", _format_ms(latency.avg_generator_ms)),
        _stat("Avg critic", _format_ms(latency.avg_critic_ms)),
        cls="ops-stat-line",
    )
    return Div(
        H3("Latency", cls="overview-subsection-title"),
        summary,
        cls="overview-panel overview-section",
    )


def register(rt):

    @rt("/compass/operations")
    async def get_operations_page(request: Request, range: str = "7d"):
        user, deny = require_compass_admin(request)
        if deny:
            return deny

        # ``from`` is a Python keyword, so read the custom lower bound straight
        # off the query string — same as Overview/Conversations. Operations
        # renders the custom-date control, so it must honor the param too (it
        # was silently ignored before, falling every custom span back to 7d).
        custom_since = request.query_params.get("from", "")
        _, since = resolve_since(range, custom_since)
        api_key_id = Config().compass_cost_api_key_id

        empty_cost = CostStats(
            total_cost=0.0,
            cost_per_session=0.0,
            cost_per_turn=0.0,
            avg_tokens_per_session=0,
            avg_tokens_per_turn=0,
            avg_rounds_per_session=0.0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            requests=0,
            sessions=0,
            turns=0,
            cache_hit_rate=0.0,
            cache_savings=0.0,
            unpriced_rows=0,
            unpriced_tokens=0,
            unpriced_token_share=0.0,
        )
        (
            cost,
            cost_by_phase,
            cost_by_model,
            trend,
            observed_models,
            api_keys,
            latency,
        ) = await asyncio.gather(
            run_in_thread(
                safe,
                lambda: get_cost_stats(since, api_key_id=api_key_id),
                empty_cost,
                name="get_cost_stats",
            ),
            run_in_thread(
                safe,
                lambda: get_cost_by_phase(since, api_key_id=api_key_id),
                [],
                name="get_cost_by_phase",
            ),
            run_in_thread(
                safe,
                lambda: get_cost_by_model(since, api_key_id=api_key_id),
                [],
                name="get_cost_by_model",
            ),
            run_in_thread(
                safe,
                lambda: get_cost_trend(since, api_key_id=api_key_id),
                [],
                name="get_cost_trend",
            ),
            run_in_thread(
                safe,
                lambda: get_observed_model_config(api_key_id=api_key_id),
                [],
                name="get_observed_model_config",
            ),
            run_in_thread(
                safe,
                lambda: get_recent_api_keys(key_id=api_key_id),
                [],
                name="get_recent_api_keys",
            ),
            run_in_thread(
                safe,
                lambda: get_latency_stats(since),
                LatencyStats(0, 0, 0),
                name="get_latency_stats",
            ),
        )

        content = Div(
            Div(
                # Operations reads only the lower bound (?from=); it never reads
                # ?to=, so hide the upper-bound input rather than render one that
                # would be silently dropped on submit.
                range_selector(
                    range, "/compass/operations", custom_since, with_upper_bound=False
                ),
                cls="strip-range",
            ),
            _costs_section(cost, api_keys, api_key_id=api_key_id),
            _usage_section(cost),
            _observed_model_section(observed_models),
            _api_key_section(api_keys),
            _breakdown_section(trend, cost_by_phase, cost_by_model),
            _latency_section(latency),
            cls="compass-overview-wrap",
        )

        return Layout(
            "Compass — Operations",
            "Starling-only: cost, tokens, latency.",
            content,
            section="compass",
            sub_nav="/compass/operations",
            user=user,
            show_heading=False,
        )

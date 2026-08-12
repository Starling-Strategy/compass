"""Data Universe — /compass/data-universe

A plain-language presentation of the data Compass draws on: the headline
counts, one row per source with when it was last refreshed into the database,
and the coverage/history breakdown. There is no sync-health apparatus here —
data flows NCTQ → Databricks (nightly) → production → staging, so the only
honest per-source signal is "Last updated" = the most recent refresh date.
"""

import asyncio
import logging
from datetime import datetime, UTC

from fasthtml.common import Div, H2, H3, P
from starlette.requests import Request

from nctqai.components.compass.bars import bar_row
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import require_compass, run_in_thread, safe
from nctqai.services import data_universe_stats as stats

logger = logging.getLogger(__name__)

SOURCE_RESULT_GROUPS = (
    (
        "NCTQ Policy Data",
        "NCTQ policy database",
        (
            "navigator_districts_topics",
            "navigator_answers_citations",
            "navigator_sources",
        ),
    ),
    ("Pathfinder Guidance", "NCTQ Pathfinder content", ("pathfinder_guidance",)),
    ("NCTQ Publications", "NCTQ publication library", ("publications",)),
    ("NCES National Data", "National district data", ("nces_national_data",)),
)

# Shared three-column grid for the Data Sources table (header + rows).
_SOURCE_GRID = (
    "display: grid; grid-template-columns: minmax(150px, 0.9fr) "
    "minmax(280px, 1.6fr) minmax(120px, 0.6fr); gap: 18px;"
)


def _format_count(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _as_aware(ts: datetime) -> datetime:
    """Normalize to UTC-aware so naive and aware timestamps can be compared."""
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _most_recent(timestamps) -> datetime | None:
    """Most recent non-null datetime among the given values, or None."""
    candidates = [ts for ts in timestamps if isinstance(ts, datetime)]
    if not candidates:
        return None
    return max(candidates, key=_as_aware)


def _format_absolute_date(ts, fallback: str = "—") -> str:
    """Absolute date like 'June 17, 2026'; ``fallback`` when missing."""
    if not ts:
        return fallback
    if isinstance(ts, datetime):
        return ts.strftime("%B %-d, %Y")
    return str(ts)


def _bar_chart(items: list[dict], name_key: str, count_key: str):
    """Render a list of items as horizontal bars."""
    if not items:
        return P("No data available.", style="color: var(--text-muted); font-size: 0.8rem;")
    max_val = max(item[count_key] for item in items) if items else 1
    return Div(
        *[bar_row(str(item[name_key]), item[count_key], max_val) for item in items],
        cls="bar-chart",
    )


def _data_inventory_tile(value, label: str, subtitle: str = ""):
    return Div(
        Div(
            _format_count(value),
            style="font-size: 1.35rem; font-weight: 800; color: var(--text-primary);",
        ),
        Div(
            label,
            style=(
                "font-size: 0.68rem; color: var(--text-muted); "
                "text-transform: uppercase; font-weight: 750;"
            ),
        ),
        *(
            [
                Div(
                    subtitle,
                    style="font-size: 0.72rem; color: var(--text-faint); margin-top: 2px;",
                )
            ]
            if subtitle
            else []
        ),
        style=(
            "padding: 14px 16px; border: 1px solid var(--border-card, #e8ecf1); "
            "border-radius: 8px; background: var(--bg-card, #fff); "
            "box-shadow: 0 1px 2px rgba(15,34,58,0.04);"
        ),
    )


def _overview_band(
    *,
    summary: dict,
    publications: dict,
    nces: dict,
    pathfinder: dict,
    as_of: str,
):
    """The lead card: a plain-language summary of what Compass draws on, plus a
    single freshness line for the whole dataset."""
    return Div(
        Div(
            "Compass data at a glance",
            style=(
                "font-size: 0.72rem; color: var(--text-muted); "
                "text-transform: uppercase; font-weight: 750; margin-bottom: 8px;"
            ),
        ),
        P(
            f"Compass draws on {_format_count(summary.get('answer_count', 0))} reviewed "
            f"policy answers covering {_format_count(summary.get('district_count', 0))} "
            f"districts across {_format_count(summary.get('state_count', 0))} states, "
            f"backed by {_format_count(summary.get('source_document_count', 0))} source "
            f"documents. It also includes Pathfinder guidance "
            f"({_format_count(pathfinder.get('total', 0))} items), "
            f"{_format_count(publications.get('total', 0))} NCTQ publications, and "
            f"{_format_count(nces.get('nces_total', 0))} districts of NCES national data.",
            style=(
                "color: var(--text-secondary); font-size: 0.92rem; line-height: 1.55; "
                "margin: 0; max-width: 760px;"
            ),
        ),
        *(
            [
                Div(
                    f"Data current as of {as_of}",
                    style=(
                        "font-size: 0.8rem; color: var(--text-muted); "
                        "font-weight: 600; margin-top: 14px;"
                    ),
                )
            ]
            if as_of
            else []
        ),
        style=(
            "background: var(--bg-card, #fff); border: 1px solid var(--border-card, #e8ecf1); "
            "border-radius: 8px; padding: 22px 24px; margin-bottom: 24px; "
            "box-shadow: 0 1px 2px rgba(15,34,58,0.05);"
        ),
    )


def _inventory_summary_section(
    *,
    summary: dict,
    publications: dict,
    nces: dict,
    pathfinder: dict,
):
    return Div(
        H2("Data Available to Compass", cls="overview-section-title"),
        P(
            "What Compass can use right now.",
            cls="overview-section-subtitle",
        ),
        Div(
            _data_inventory_tile(
                summary.get("answer_count", 0),
                "Policy answers",
                f"{summary.get('year_count', 0)} academic-year ranges",
            ),
            _data_inventory_tile(
                summary.get("source_document_count", 0),
                "Source documents",
                "Navigator citations and evidence",
            ),
            _data_inventory_tile(
                pathfinder.get("total", 0),
                "Pathfinder items",
                (
                    f"{_format_count(pathfinder.get('topic_pages', 0))} topic pages; "
                    f"{_format_count(pathfinder.get('articles', 0))} articles"
                ),
            ),
            _data_inventory_tile(
                publications.get("total", 0),
                "NCTQ publications",
                f"{publications.get('coverage_pct', 0)}% summarized",
            ),
            _data_inventory_tile(
                nces.get("nces_total", 0),
                "NCES districts",
                f"{nces.get('linked_count', 0)} linked to Navigator",
            ),
            _data_inventory_tile(
                summary.get("district_count", 0),
                "Covered districts",
                f"{summary.get('state_count', 0)} states",
            ),
            style=(
                "display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); "
                "gap: 12px; margin-top: 12px;"
            ),
        ),
        cls="overview-section",
    )


def _source_inventory_text(title: str, inventory: dict) -> str:
    if title == "NCTQ Policy Data":
        return (
            f"{_format_count(inventory.get('answers'))} answers across "
            f"{_format_count(inventory.get('years'))} academic-year ranges; "
            f"{_format_count(inventory.get('districts'))} districts; "
            f"{_format_count(inventory.get('sources'))} source documents."
        )
    if title == "Pathfinder Guidance":
        return (
            f"{_format_count(inventory.get('total'))} website items: "
            f"{_format_count(inventory.get('topic_pages'))} topic pages, "
            f"{_format_count(inventory.get('pages'))} pages, "
            f"{_format_count(inventory.get('articles'))} articles."
        )
    if title == "NCTQ Publications":
        return (
            f"{_format_count(inventory.get('total'))} publications; "
            f"{_format_count(inventory.get('summarized'))} ready summaries."
        )
    if title == "NCES National Data":
        return (
            f"{_format_count(inventory.get('total'))} national district records; "
            f"{_format_count(inventory.get('linked'))} linked to Navigator districts."
        )
    return "Inventory details unavailable."


def _data_source_row(
    *,
    title: str,
    source_system: str,
    inventory: dict,
    last_updated,
):
    return Div(
        Div(
            Div(title, style="font-weight: 750; font-size: 0.9rem;"),
            Div(
                source_system,
                style="font-size: 0.72rem; color: var(--text-faint); margin-top: 2px;",
            ),
        ),
        Div(
            _source_inventory_text(title, inventory),
            style="font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45;",
        ),
        Div(
            _format_absolute_date(last_updated, fallback="Unknown"),
            style="font-size: 0.82rem; color: var(--text-secondary); font-weight: 600;",
        ),
        style=(
            _SOURCE_GRID
            + " align-items: start; padding: 14px 0; "
            "border-bottom: 1px solid var(--border-light, #f1f5f9);"
        ),
    )


def _data_sources_section(
    *,
    inventory_by_source: dict[str, dict],
    source_last_updated: dict,
):
    header = Div(
        Div("Data source", style="font-weight: 700;"),
        Div("What Compass has", style="font-weight: 700;"),
        Div("Last updated", style="font-weight: 700;"),
        style=(
            _SOURCE_GRID
            + " color: var(--text-muted); font-size: 0.72rem; text-transform: uppercase;"
        ),
    )
    rows = [
        _data_source_row(
            title=title,
            source_system=source_system,
            inventory=inventory_by_source.get(title, {}),
            last_updated=source_last_updated.get(title),
        )
        for title, source_system, _categories in SOURCE_RESULT_GROUPS
    ]
    return Div(
        H2("Data Sources", cls="overview-section-title"),
        P(
            "When each source was last updated.",
            cls="overview-section-subtitle",
        ),
        Div(header, *rows, cls="overview-panel"),
        cls="overview-section",
    )


async def compose_data_universe_content() -> Div:
    """Build the inner Data Universe content block (no Layout wrap).

    Runs the eight independent stats reads concurrently on worker threads —
    ``safe`` lets any one branch fail without taking out the whole page.
    """
    (
        summary,
        publications,
        nces,
        pathfinder,
        districts_by_state,
        questions_by_topic,
        answers_by_year,
        sync_ts,
    ) = await asyncio.gather(
        run_in_thread(safe, stats.get_summary_stats, {}, name="get_summary_stats"),
        run_in_thread(safe, stats.get_publication_stats, {}, name="get_publication_stats"),
        run_in_thread(safe, stats.get_nces_stats, {}, name="get_nces_stats"),
        run_in_thread(safe, stats.get_pathfinder_content_stats, {}, name="get_pathfinder_content_stats"),
        run_in_thread(safe, stats.get_districts_by_state, [], name="get_districts_by_state"),
        run_in_thread(safe, stats.get_questions_by_topic, [], name="get_questions_by_topic"),
        run_in_thread(safe, stats.get_answers_by_year, [], name="get_answers_by_year"),
        run_in_thread(safe, stats.get_sync_timestamps, {}, name="get_sync_timestamps"),
    )

    # ── Per-source inventory (feeds the "What Compass has" column) ────────────
    inventory_by_source = {
        "NCTQ Policy Data": {
            "answers": summary.get("answer_count", 0),
            "years": summary.get("year_count", 0),
            "districts": summary.get("district_count", 0),
            "states": summary.get("state_count", 0),
            "questions": summary.get("question_count", 0),
            "topics": summary.get("topic_count", 0),
            "sources": summary.get("source_document_count", 0),
        },
        "Pathfinder Guidance": {
            "total": pathfinder.get("total", 0),
            "topic_pages": pathfinder.get("topic_pages", 0),
            "pages": pathfinder.get("pages", 0),
            "articles": pathfinder.get("articles", 0),
        },
        "NCTQ Publications": {
            "total": publications.get("total", 0),
            "summarized": publications.get("summarized", 0),
            "coverage_pct": publications.get("coverage_pct", 0),
        },
        "NCES National Data": {
            "total": nces.get("nces_total", 0),
            "linked": nces.get("linked_count", 0),
        },
    }

    # ── Per-source "Last updated" = the refresh date Databricks wrote ─────────
    source_last_updated = {
        "NCTQ Policy Data": sync_ts.get("policy_data_synced"),
        "Pathfinder Guidance": pathfinder.get("synced_at"),
        "NCTQ Publications": sync_ts.get("publications_synced"),
        "NCES National Data": sync_ts.get("nces_imported"),
    }
    as_of_label = _format_absolute_date(
        _most_recent(source_last_updated.values()), fallback=""
    )

    overview = _overview_band(
        summary=summary,
        publications=publications,
        nces=nces,
        pathfinder=pathfinder,
        as_of=as_of_label,
    )

    inventory_section = _inventory_summary_section(
        summary=summary,
        publications=publications,
        nces=nces,
        pathfinder=pathfinder,
    )

    sources_section = _data_sources_section(
        inventory_by_source=inventory_by_source,
        source_last_updated=source_last_updated,
    )

    # ── Coverage Detail ──────────────────────────────────────
    state_chart = _bar_chart(districts_by_state, "state", "count")
    topic_chart = _bar_chart(questions_by_topic, "topic", "count")

    coverage_section = Div(
        H2("Coverage Detail", cls="overview-section-title"),
        P(
            "Where covered districts are located and which policy topics have "
            "the most tracked questions.",
            cls="overview-section-subtitle",
        ),
        Div(
            Div(
                H3("Districts by State", cls="overview-subsection-title"),
                state_chart,
                cls="overview-panel",
            ),
            Div(
                H3("Questions by Topic", cls="overview-subsection-title"),
                topic_chart,
                cls="overview-panel",
            ),
            cls="overview-grid-2",
        ),
        cls="overview-section",
    )

    # ── Historical Depth ─────────────────────────────────────
    year_chart = _bar_chart(answers_by_year, "academic_year", "count")
    year_count = summary.get("year_count", 0)
    earliest_year = summary.get("earliest_academic_year")

    year_section = Div(
        H2("Coverage Over Time", cls="overview-section-title"),
        P(
            "Reviewed policy answers by academic year"
            + (f", starting with {earliest_year}" if earliest_year else "")
            + f". Compass currently has coverage across {year_count} academic years.",
            cls="overview-section-subtitle",
        ),
        Div(
            Div(year_chart, cls="overview-panel"),
            cls="overview-grid-1",
        ),
        P(
            "Each number is the total policy answers reviewed for that academic "
            "year across all covered districts. It counts answers that have a "
            "specific value, along with answers marked Issue not addressed, Not "
            "applicable for district, or Unavailable, because each one reflects a "
            "district and question that an analyst has reviewed.",
            cls="overview-section-subtitle",
        ),
        cls="overview-section",
    )

    # ── Assemble ─────────────────────────────────────────────
    content = Div(
        overview,
        inventory_section,
        sources_section,
        coverage_section,
        year_section,
    )

    return content


def register(rt):

    @rt("/compass/data-universe")
    async def get_data_universe(request: Request):
        """Data Universe — deep route after the v3 IA change.

        Old bookmarks land here and still see the page they expect.
        """
        user, deny = require_compass(request)
        if deny:
            return deny

        return Layout(
            "Data Universe",
            "",
            await compose_data_universe_content(),
            section="compass",
            sub_nav="/compass/data-universe",
            user=user,
            show_heading=False,
        )

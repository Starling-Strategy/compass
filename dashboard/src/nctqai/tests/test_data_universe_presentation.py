"""Data Universe is a clean presentation of the data Compass uses (#1816).

The page used to carry a daily-check / sync-status apparatus (per-source run
status, "needs attention", durations, run ids, freshness color coding). Data
now flows NCTQ → Databricks (nightly) → production → staging, so that machinery
was stale and misleading. The page keeps ONE honest signal per source —
"Last updated" = the most recent refresh date — plus a single dataset-wide
"Data current as of" line.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import fasthtml.common as fh


def _render() -> str:
    """Render compose_data_universe_content with mocked stats, return its HTML."""
    with patch("nctqai.routes.compass.data_universe.stats") as mock_stats:
        mock_stats.get_summary_stats.return_value = {
            "district_count": 133, "state_count": 51, "question_count": 0,
            "topic_count": 27, "year_count": 11, "answer_count": 47244,
            "source_document_count": 7445, "earliest_academic_year": "2015-16",
        }
        mock_stats.get_publication_stats.return_value = {
            "total": 747, "summarized": 698, "coverage_pct": 93,
        }
        mock_stats.get_nces_stats.return_value = {
            "nces_total": 19530, "linked_count": 148,
        }
        mock_stats.get_pathfinder_content_stats.return_value = {
            "total": 2751, "topic_pages": 7, "pages": 46, "articles": 2698,
            "synced_at": datetime(2026, 6, 15, 4, 0, tzinfo=UTC),
        }
        mock_stats.get_districts_by_state.return_value = [{"state": "CA", "count": 12}]
        mock_stats.get_questions_by_topic.return_value = [{"topic": "Tenure", "count": 9}]
        mock_stats.get_answers_by_year.return_value = [{"academic_year": "2024-25", "count": 4000}]
        mock_stats.get_sync_timestamps.return_value = {
            "policy_data_synced": datetime(2026, 6, 17, 5, 0, tzinfo=UTC),
            "sources_synced": datetime(2026, 6, 17, 5, 0, tzinfo=UTC),
            "publications_synced": datetime(2026, 6, 10, 5, 0, tzinfo=UTC),
            "nces_imported": datetime(2026, 5, 1, 5, 0, tzinfo=UTC),
        }

        from nctqai.routes.compass.data_universe import compose_data_universe_content
        result = asyncio.run(compose_data_universe_content())

    return fh.to_xml(result)


def test_per_source_last_updated_column_and_dates_appear():
    """The Data Sources table has a 'Last updated' column and shows each
    source's most-recent refresh as a plain absolute date."""
    html = _render()
    assert "Last updated" in html
    # Most recent among policy/pathfinder/publications/nces is 2026-06-17.
    assert "June 17, 2026" in html
    # NCES's older refresh still renders its own date.
    assert "May 1, 2026" in html


def test_data_current_as_of_line_appears():
    """The overview band carries a single dataset-wide freshness line set to the
    most recent per-source timestamp."""
    html = _render()
    assert "Data current as of June 17, 2026" in html


def test_no_daily_check_ui_remains():
    """None of the removed sync-health language should survive."""
    html = _render()
    for gone in (
        "Needs attention",
        "Daily check",
        "Latest run",
        "Latest daily run",
        "Compass Data Health",
        "Show inventory breakdown",
    ):
        assert gone not in html, gone


def test_missing_timestamps_degrade_to_fallback():
    """With no timestamps at all, the dataset-wide freshness line is omitted
    (no glitchy 'as of —') and each source row falls back to 'Unknown' rather
    than crashing."""
    with patch("nctqai.routes.compass.data_universe.stats") as mock_stats:
        mock_stats.get_summary_stats.return_value = {}
        mock_stats.get_publication_stats.return_value = {}
        mock_stats.get_nces_stats.return_value = {}
        mock_stats.get_pathfinder_content_stats.return_value = {}
        mock_stats.get_districts_by_state.return_value = []
        mock_stats.get_questions_by_topic.return_value = []
        mock_stats.get_answers_by_year.return_value = []
        mock_stats.get_sync_timestamps.return_value = {}

        from nctqai.routes.compass.data_universe import compose_data_universe_content
        html = fh.to_xml(asyncio.run(compose_data_universe_content()))

    assert "Data current as of" not in html
    assert "Unknown" in html

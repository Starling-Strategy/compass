"""Regression coverage for separately tracked tables, CSV exports, and charts."""

from fasthtml.common import to_xml

from nctqai.components.compass.conversation_list import _TRIAGE_TABS, _TRIAGE_TITLES
from nctqai.routes.compass.overview import (
    _artifact_card,
    _artifact_href,
    _engagement_section,
)
from nctqai.services.compass_stats import ArtifactCoverage, EngagementStats


def _coverage() -> ArtifactCoverage:
    return ArtifactCoverage(
        sessions=1_000,
        with_table=270,
        table_pct=27,
        with_csv_export=250,
        csv_export_pct=25,
        with_chart=80,
        chart_pct=8,
    )


def test_get_artifact_coverage_counts_each_saved_artifact_separately(monkeypatch):
    from nctqai.services import compass_stats

    captured = {}

    def fake_run_sql(sql, binds):
        captured["sql"] = sql
        return [
            {
                "sessions": 1_000,
                "with_table": 270,
                "with_csv_export": 250,
                "with_chart": 80,
            }
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)
    monkeypatch.setattr(compass_stats, "artifact_summary_v2_available", lambda: True)

    result = compass_stats.get_artifact_coverage()

    assert result == _coverage()
    assert "has_table" in captured["sql"]
    assert "has_csv_export" in captured["sql"]
    assert "has_chart" in captured["sql"]
    assert "FROM scoped" in captured["sql"]


def test_get_artifact_coverage_zero_sessions_is_honest(monkeypatch):
    from nctqai.services import compass_stats

    monkeypatch.setattr(
        compass_stats,
        "run_sql",
        lambda sql, binds: [
            {"sessions": 0, "with_table": 0, "with_csv_export": 0, "with_chart": 0}
        ],
    )
    monkeypatch.setattr(compass_stats, "artifact_summary_v2_available", lambda: True)
    result = compass_stats.get_artifact_coverage()
    assert result == ArtifactCoverage(0, 0, 0)


def test_artifact_card_says_unavailable_instead_of_showing_zero_percent():
    html = to_xml(
        _artifact_card(
            coverage=ArtifactCoverage(100, 0, 0, available=False),
            label="Charts shown",
            count=0,
            pct=0,
            href="/compass/conversations?tab=has-chart",
        )
    )
    assert "Saved artifact summaries are unavailable" in html
    assert "0%" not in html


def test_artifact_card_names_one_artifact_and_full_conversation_denominator():
    html = to_xml(
        _artifact_card(
            coverage=_coverage(),
            label="Charts shown",
            count=80,
            pct=8,
            href="/compass/conversations?tab=has-chart",
        )
    )
    assert "Charts shown" in html
    assert "8%" in html
    assert "80 of 1,000 conversations" in html
    assert "tab=has-chart" in html


def test_artifact_href_carries_artifact_and_active_range():
    assert _artifact_href("has-csv-export", "30d", "", "", False) == (
        "/compass/conversations?tab=has-csv-export&range=30d"
    )
    custom = _artifact_href("has-chart", "custom", "2026-06-01", "2026-06-15", False)
    assert "tab=has-chart" in custom
    assert "range=custom" in custom
    assert "from=2026-06-01" in custom
    assert "to=2026-06-15" in custom


def test_engagement_section_renders_separate_artifact_cards():
    html = to_xml(
        _engagement_section(
            EngagementStats(1_000, 2.0, 40, 20),
            None,
            coverage=_coverage(),
        )
    )
    assert "Data tables" in html
    assert "CSV exports available" in html
    assert "Charts shown" in html
    assert "27%" in html and "25%" in html and "8%" in html


def test_artifact_filters_have_distinct_labels_and_definitions():
    labels = dict(_TRIAGE_TABS)
    assert labels["has-table"] == "Has data table"
    assert labels["has-csv-export"] == "Has CSV export"
    assert labels["has-chart"] == "Has chart"
    assert "saved non-empty data table" in _TRIAGE_TITLES["has-table"]
    assert "saved CSV export" in _TRIAGE_TITLES["has-csv-export"]
    assert "saved chart" in _TRIAGE_TITLES["has-chart"]

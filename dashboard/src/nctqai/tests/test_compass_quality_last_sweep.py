"""Tests for the "last successful sweep" chrome strip (issue #751).

The scorecard plumbs DimensionScore.latest_finished_at but the dashboard
chrome threw it away — it showed the sweep UUID, not a timestamp. These
tests pin the new chrome.last_sweep_strip component and its relative-time
helper, plus the hero wiring that reads max(latest_finished_at).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fasthtml.common as fh

from compass_backend.quality.scorecard_models import (
    CANONICAL_DIM_SLUGS,
    BuildContext,
    DimensionScore,
    ScorecardSnapshot,
)
from nctqai.components.compass.quality.chrome import (
    humanize_age,
    last_sweep_strip,
    latest_finished_across,
)


# ── humanize_age ──────────────────────────────────────────────────────────

def test_humanize_age_minutes():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(minutes=5)).isoformat()
    assert humanize_age(ts, now=now) == "5 minutes ago"


def test_humanize_age_singular_hour():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1, minutes=2)).isoformat()
    assert humanize_age(ts, now=now) == "1 hour ago"


def test_humanize_age_days():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(days=3, hours=1)).isoformat()
    assert humanize_age(ts, now=now) == "3 days ago"


def test_humanize_age_just_now():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(seconds=10)).isoformat()
    assert humanize_age(ts, now=now) == "just now"


def test_humanize_age_postgres_space_separated():
    """Postgres `finished_at::text` uses a space and a 2-digit offset."""
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    # e.g. "2026-06-03 10:00:00+00"
    ts = "2026-06-03 10:00:00+00"
    assert humanize_age(ts, now=now) == "2 hours ago"


def test_humanize_age_unparseable_returns_none():
    assert humanize_age("not-a-timestamp") is None


def test_humanize_age_none_returns_none():
    assert humanize_age(None) is None


# ── latest_finished_across ────────────────────────────────────────────────

def _snapshot_with_finished(values: list[str | None]) -> ScorecardSnapshot:
    dims = []
    for slug, finished in zip(CANONICAL_DIM_SLUGS, values):
        dims.append(DimensionScore(
            dim_slug=slug,
            name=slug.replace("-", " ").title(),
            definition="d",
            score_pct=0,
            n_trials=0,
            regressed=False,
            latest_finished_at=finished,
        ))
    return ScorecardSnapshot(
        build=BuildContext(
            build_id="b",
            sweep_id="s",
            criterion_set_version="v1",
            cases=0,
            trials=0,
        ),
        dimensions=dims,
    )


def test_latest_finished_across_picks_max():
    values = [
        "2026-06-01T00:00:00+00:00",
        "2026-06-03T08:00:00+00:00",  # max
        None,
        "2026-05-30T00:00:00+00:00",
        None, None, None,
    ]
    snap = _snapshot_with_finished(values)
    assert latest_finished_across(snap.dimensions) == "2026-06-03T08:00:00+00:00"


def test_latest_finished_across_all_none():
    snap = _snapshot_with_finished([None] * 7)
    assert latest_finished_across(snap.dimensions) is None


# ── last_sweep_strip component ────────────────────────────────────────────

def test_last_sweep_strip_renders_relative_age():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=2)).isoformat()
    strip = last_sweep_strip(ts, now=now)
    html = fh.to_xml(strip)
    assert "Last successful sweep" in html
    assert "2 hours ago" in html
    assert "quality-last-sweep-strip" in html


def test_last_sweep_strip_renders_absolute_timestamp_in_title():
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=2)).isoformat()
    strip = last_sweep_strip(ts, now=now)
    html = fh.to_xml(strip)
    # the absolute timestamp is exposed (title attr) for hover precision
    assert "2026-06-03" in html


def test_last_sweep_strip_no_data():
    strip = last_sweep_strip(None)
    html = fh.to_xml(strip)
    assert "quality-last-sweep-strip" in html
    assert "no successful sweep" in html.lower()


def test_last_sweep_strip_unparseable_falls_back_to_raw():
    strip = last_sweep_strip("garbage-value")
    html = fh.to_xml(strip)
    # should not crash; surfaces the raw value rather than a relative age
    assert "garbage-value" in html

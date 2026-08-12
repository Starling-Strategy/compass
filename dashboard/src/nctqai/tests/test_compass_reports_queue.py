"""Tests for the Compass reports queue (P2, #1349).

Two surfaces:
  - ``list_reports`` SQL builder — filter precedence / open-queue defaulting.
    The DB call is monkeypatched; we assert the generated SQL + binds.
  - The ``/compass/quality/reports`` route HTML shape — the read service is
    patched to return fixture rows so routes render with known data, mirroring
    ``test_compass_quality_routes.py``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import fasthtml.common as fh

from nctqai.routes.compass.quality import reports as route
from nctqai.services.compass_quality import reports as svc
from nctqai.services.compass_quality.reports import CaseReport


# ── Fixtures ──────────────────────────────────────────────────────────────


def _report(
    *,
    rid: str = "11111111-1111-1111-1111-111111111111",
    case_id: int | None = 42,
    session_id: str = "22222222-2222-2222-2222-222222222222",
    dimension: str | None = "sort-accuracy",
    outcome: str = "fail",
    status: str = "open",
    comments: str | None = "short comment",
) -> CaseReport:
    ts = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    return CaseReport(
        id=UUID(rid),
        case_id=case_id,
        session_id=UUID(session_id),
        turn_index=1,
        trace_id="tr-1",
        dimension=dimension,
        outcome=outcome,
        comments=comments,
        status=status,
        reviewer="macon",
        linked_issue=None,
        created_at=ts,
        updated_at=ts,
    )


class _FakeUser:
    email = "macon@starlingstrategy.com"
    name = "Macon"
    role = "admin"

    def can_access(self, _section: str) -> bool:
        return True

    def is_admin(self) -> bool:
        # Reports is admin-only (require_compass_admin, #1403); this double
        # represents an admin triager, so it honors the real User contract.
        return self.role == "admin"


class _FakeQueryParams(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _FakeRequest:
    class _state:
        user = _FakeUser()

    state = _state()
    headers: dict = {}

    def __init__(self, query_params: dict | None = None):
        self.query_params = _FakeQueryParams(query_params or {})


# ── list_reports SQL builder ──────────────────────────────────────────────


def _capture_sql(monkeypatch):
    """Patch run_sql in the service module; return a list that captures
    ``(sql, binds)`` tuples and yields no rows."""
    captured: list[tuple[str, tuple]] = []

    def fake_run_sql(sql, params=()):
        captured.append((sql, params))
        return []

    monkeypatch.setattr(svc, "run_sql", fake_run_sql)
    return captured


def test_list_reports_default_is_open_queue(monkeypatch):
    """No filters → open queue: fail/partial AND not resolved/wontfix."""
    captured = _capture_sql(monkeypatch)
    svc.list_reports()
    sql, binds = captured[0]
    assert "outcome IN ('fail','partial')" in sql
    assert "status <> ALL(%s)" in sql
    # the closed-status bind list rides in the params (#1806 added
    # reviewed_no_followup as a terminal/closed state hidden from the open queue)
    assert ["resolved", "wontfix", "reviewed_no_followup"] in binds
    assert "ORDER BY created_at DESC" in sql


def test_list_reports_explicit_status_overrides_open_default(monkeypatch):
    """An explicit status drops the open-queue narrowing so a reviewer can
    inspect e.g. resolved reports."""
    captured = _capture_sql(monkeypatch)
    svc.list_reports(status="resolved")
    sql, binds = captured[0]
    assert "status = %s" in sql
    assert "resolved" in binds
    # open-queue narrowing must NOT be applied
    assert "outcome IN ('fail','partial')" not in sql


def test_list_reports_dimension_and_outcome_filters(monkeypatch):
    captured = _capture_sql(monkeypatch)
    svc.list_reports(outcome="partial", dimension="citation")
    sql, binds = captured[0]
    assert "outcome = %s" in sql
    assert "dimension = %s" in sql
    assert "partial" in binds
    assert "citation" in binds


def test_list_reports_unknown_status_is_ignored(monkeypatch):
    """A junk status value (off the URL) is treated as no filter, so the open
    queue default still applies — no SQL error, no injected literal."""
    captured = _capture_sql(monkeypatch)
    svc.list_reports(status="not-a-real-status")
    sql, _binds = captured[0]
    assert "status = %s" not in sql
    assert "outcome IN ('fail','partial')" in sql


def test_status_counts_zero_fills_all_statuses(monkeypatch):
    monkeypatch.setattr(svc, "run_sql", lambda sql, params=(): [{"status": "open", "n": 2}])
    counts = svc.status_counts()
    assert counts["open"] == 2
    # every CHECK value present even with no rows
    for s in svc.STATUSES:
        assert s in counts
    assert counts["wontfix"] == 0


def test_status_counts_default_scopes_to_actionable_outcomes(monkeypatch):
    """Default counts use the same actionable-outcome scope as the open queue,
    so the ``open`` pill reconciles with the rows shown (an open outcome='pass'
    flag the queue hides must not inflate the ``open`` count). It must NOT apply
    the queue's status exclusion — closed statuses still appear in the pills."""
    captured = _capture_sql(monkeypatch)
    svc.status_counts()
    sql, _binds = captured[0]
    assert "outcome IN ('fail','partial')" in sql
    assert "GROUP BY status" in sql
    # never narrows the status facet itself — wontfix/resolved stay countable
    assert "status = %s" not in sql
    assert "status <> ALL(%s)" not in sql


def test_status_counts_explicit_outcome_overrides_default_scope(monkeypatch):
    """An explicit outcome filter (e.g. pass) drops the actionable default so the
    pills reflect exactly the population the filtered list shows."""
    captured = _capture_sql(monkeypatch)
    svc.status_counts(outcome="pass")
    sql, binds = captured[0]
    assert "outcome = %s" in sql
    assert "pass" in binds
    assert "outcome IN ('fail','partial')" not in sql


def test_status_counts_applies_dimension_filter(monkeypatch):
    captured = _capture_sql(monkeypatch)
    svc.status_counts(dimension="citation")
    sql, binds = captured[0]
    assert "dimension = %s" in sql
    assert "citation" in binds


# ── Route HTML shape ──────────────────────────────────────────────────────


def _patch_reads(monkeypatch, reports):
    monkeypatch.setattr(route, "list_reports", lambda **kw: reports)
    monkeypatch.setattr(
        route,
        "status_counts",
        lambda **kw: {"open": 1, "triaged": 1, "promoted": 0, "resolved": 3, "wontfix": 0},
    )
    monkeypatch.setattr(
        route, "distinct_dimensions", lambda: ["citation-accuracy", "sort-accuracy"]
    )


def test_reports_page_renders_queue_table(monkeypatch):
    _patch_reads(monkeypatch, [_report()])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    # DASH-R6 relabel: the page is titled "Flagged Issues" (the route + URL stay
    # /compass/quality/reports). The old "Compass Reports Queue" title is gone.
    assert "Flagged Issues" in html
    assert "Compass Reports Queue" not in html
    assert "quality-reports-table" in html
    assert "quality-verdict-fail" in html  # outcome color-coding


def test_reports_page_has_flagged_and_updated_columns(monkeypatch):
    """DASH-R6 dated columns: 'Flagged' (created_at) + an honest 'Updated'
    (updated_at). Migration 099 stores one rolling updated_at, so this is the
    single honest "last changed" date — no fabricated Seen/Fixed per-transition
    dates (HM-R1)."""
    # Distinct created_at vs updated_at so both dates are visibly rendered.
    r = _report()
    r = CaseReport(
        **{
            **r.__dict__,
            "created_at": datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 6, 9, 9, 15, tzinfo=timezone.utc),
        }
    )
    _patch_reads(monkeypatch, [r])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    # Both column headers present, in the honest vocabulary.
    assert ">Flagged</th>" in html
    assert ">Updated</th>" in html
    # The two distinct timestamps render (Flagged = created_at, Updated = updated_at).
    assert "Jun 5, 2026, 10:30 AM ET" in html
    assert "Jun 9, 2026, 5:15 AM ET" in html
    # No fabricated transition-date vocabulary on the surface.
    assert "Seen" not in html
    assert "Fixed" not in html


def test_reports_page_row_has_conversation_backlink(monkeypatch):
    """Each row links to the in-dashboard conversation detail so a reviewer
    jumps from a flag to the conversation it's about."""
    _patch_reads(monkeypatch, [_report()])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    assert (
        'href="/compass/conversations/22222222-2222-2222-2222-222222222222"' in html
    )
    assert "View conversation" in html


def test_reports_table_has_aria_live(monkeypatch):
    """The status-change swap target carries aria-live so a screen reader hears
    the table update; aria-busy starts 'false' and is toggled during the swap."""
    _patch_reads(monkeypatch, [_report()])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    # aria-live on the table swap target.
    assert 'aria-live="polite"' in html
    assert 'aria-busy="false"' in html
    # The per-row status form toggles aria-busy on the table around the swap.
    assert "quality-reports-table" in html
    assert "setAttribute('aria-busy','true')" in html


def test_reports_empty_state_relabeled_and_aria_live(monkeypatch):
    """Zero flags → the no-rows state reads in the Flagged Issues vocabulary and
    still carries aria-live (it keeps the #quality-reports-table swap id)."""
    _patch_reads(monkeypatch, [])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    assert "No flagged issues match these filters." in html
    assert 'id="quality-reports-table"' in html
    assert 'aria-live="polite"' in html


def test_reports_page_row_has_debug_ticket_link(monkeypatch):
    _patch_reads(monkeypatch, [_report(case_id=42)])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    # & is HTML-escaped to &amp; in the rendered attribute
    assert "staging-compass.nctq.ai/?debug=true&amp;case_id=42" in html
    # W2-5: durable links use case_id only — the ?session= fossil is gone.
    assert "session=" not in html


def test_reports_page_omits_ticket_when_case_absent(monkeypatch):
    """W2-5: a report with no case_id has no durable ticket to point at.

    The old behavior fell back to ``?session=<id>`` (a fossil that replays a
    stored turn). Now the row omits the "Open ticket" link entirely — the
    "View conversation" backlink remains the fallback — rather than emitting a
    dead bare ``?debug=true`` link.
    """
    _patch_reads(monkeypatch, [_report(case_id=None)])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    # No ticket link rendered (header column is "Ticket"; the link text is
    # "Open ticket", so its absence is unambiguous).
    assert "Open ticket" not in html
    # And definitely no fossil session= or bare case_id= param.
    assert "session=" not in html
    assert "case_id=" not in html


def test_reports_page_status_control_posts_to_backend_proxy(monkeypatch):
    _patch_reads(monkeypatch, [_report()])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    assert "/compass/quality/reports/11111111-1111-1111-1111-111111111111/status" in html
    # every triage status offered as an option VALUE (labels may be humanized,
    # e.g. reviewed_no_followup, so assert on the value the DB CHECK accepts).
    for s in svc.STATUSES:
        assert f'value="{s}"' in html


def test_reports_page_offers_reviewed_no_followup_with_human_label(monkeypatch):
    """#1806: the new terminal status is selectable with its human label, and the
    option VALUE stays the raw status the DB CHECK + backend Literal accept."""
    _patch_reads(monkeypatch, [_report()])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    assert "reviewed_no_followup" in svc.STATUSES
    # raw value kept on the option (what gets POSTed / written)...
    assert 'value="reviewed_no_followup"' in html
    # ...but the visible text is the friendly label (HTML-escapes the en dash).
    assert "Reviewed" in html and "no follow-up" in html
    # the raw underscored token is never shown as the option label
    assert ">reviewed_no_followup</option>" not in html


def test_reports_page_renders_for_non_admin_role(monkeypatch):
    """#1806: a non-admin (analyst) reaches the Flagged Issues page — the gate is
    now the all-roles Compass section, not the admin-only eval-builder gate."""

    class _AnalystUser(_FakeUser):
        role = "analyst"

        def is_admin(self) -> bool:
            return False

    class _AnalystRequest(_FakeRequest):
        class _state:
            user = _AnalystUser()

        state = _state()

    _patch_reads(monkeypatch, [_report()])
    result = asyncio.run(route._reports_page(_AnalystRequest()))
    html = fh.to_xml(result)
    assert "Flagged Issues" in html
    assert "quality-reports-table" in html


def test_reports_page_summary_counts(monkeypatch):
    _patch_reads(monkeypatch, [_report()])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    assert "Total 5" in html  # 1+1+0+3+0
    assert "resolved 3" in html


def test_reports_page_expands_long_comments_and_linkifies_urls(monkeypatch):
    long_comment = "x" * 120 + "\nSee https://example.org/review."
    _patch_reads(monkeypatch, [_report(comments=long_comment)])
    result = asyncio.run(route._reports_page(_FakeRequest()))
    html = fh.to_xml(result)
    # The summary stays compact, but the full text is accessible without hover.
    assert "…" in html
    assert "View full comment" in html
    assert "https://example.org/review" in html
    assert 'href="https://example.org/review"' in html
    assert f'title="{long_comment}"' not in html


def test_reports_comment_linkifier_excludes_surrounding_closing_parenthesis(monkeypatch):
    comment = "Open (https://staging-compass.nctq.ai/?debug=true&case_id=12)"
    _patch_reads(monkeypatch, [_report(comments=comment)])
    html = fh.to_xml(asyncio.run(route._reports_page(_FakeRequest())))
    assert 'href="https://staging-compass.nctq.ai/?debug=true&amp;case_id=12"' in html
    assert 'case_id=12)' not in html


def test_reports_page_has_editable_friendly_dimension_selector(monkeypatch):
    _patch_reads(monkeypatch, [_report(dimension="not-sure")])
    html = fh.to_xml(asyncio.run(route._reports_page(_FakeRequest())))
    assert "/compass/quality/reports/11111111-1111-1111-1111-111111111111/dimension" in html
    assert ">Not sure</option>" in html
    assert "Not assigned" in html


def test_flag_dimensions_use_canonical_scorecard_slugs():
    from nctqai.components.compass.flag_dimensions import FLAG_DIMENSIONS

    values = dict(FLAG_DIMENSIONS)
    assert values["selection-accuracy"] == "Selection Accuracy"
    assert values["data-fidelity"] == "Data Fidelity"
    assert values["coverage-state-labeling"] == "Coverage-State Labeling"
    assert values["citation-accuracy"] == "Citation Accuracy"
    assert "selection" not in values
    assert "citation" not in values


def test_updated_queue_results_skips_filter_only_dimension_query(monkeypatch):
    """A status/dimension swap needs rows and counts, not filter options."""
    calls: list[str] = []

    def fake_list_reports(**_kw):
        return [_report()]

    def fake_status_counts(**_kw):
        return {"open": 1, "triaged": 0, "promoted": 0, "resolved": 0, "wontfix": 0}

    def fake_distinct_dimensions():
        raise AssertionError("swap refresh must not load filter-only dimensions")

    async def fake_run_in_thread(fn, *args, **kwargs):
        calls.append(fn.__name__)
        return fn(*args, **kwargs)

    monkeypatch.setattr(route, "list_reports", fake_list_reports)
    monkeypatch.setattr(route, "status_counts", fake_status_counts)
    monkeypatch.setattr(route, "distinct_dimensions", fake_distinct_dimensions)
    monkeypatch.setattr(route, "run_in_thread", fake_run_in_thread)

    result = asyncio.run(
        route._updated_queue_results(
            {"status": "", "dimension": "", "outcome": ""},
            ok=True,
            success_message="Saved.",
            error_message="Could not save.",
        )
    )
    assert "fake_list_reports" in calls
    assert "fake_status_counts" in calls
    assert "fake_distinct_dimensions" not in calls
    assert "Saved." in fh.to_xml(result)


def test_reports_status_update_proxies_to_backend(monkeypatch):
    """POST handler calls the backend client (not the DB) and re-renders."""
    _patch_reads(monkeypatch, [_report()])

    calls = {}

    def fake_update(report_id, *, status, reviewer=None):
        calls["report_id"] = str(report_id)
        calls["status"] = status
        calls["reviewer"] = reviewer
        from nctqai.services.compass_reports_client import StatusUpdateResult

        return StatusUpdateResult(ok=True, status_code=200)

    monkeypatch.setattr(route, "update_report_status", fake_update)

    class _Req(_FakeRequest):
        async def form(self):
            return {"status": "triaged", "f_status": "", "f_dimension": "", "f_outcome": ""}

    rid = "11111111-1111-1111-1111-111111111111"
    # Capture the inner POST handler by re-registering against a throwaway
    # router whose @rt decorator just stashes each handler by path.
    handlers = {}

    def fake_rt(path):
        def deco(fn):
            handlers[path] = fn
            return fn

        return deco

    route.register(fake_rt)
    status_handler = handlers["/compass/quality/reports/{report_id}/status"]
    result = asyncio.run(status_handler(_Req(), rid))
    html = fh.to_xml(result)

    assert calls["report_id"] == rid
    assert calls["status"] == "triaged"
    assert calls["reviewer"] == "macon@starlingstrategy.com"
    assert "Status updated to triaged" in html


def test_reports_status_update_htmx_failure_shows_error(monkeypatch):
    """The HTMX path (what real users hit) must surface a failed backend write.

    The status <select> swaps "#quality-reports-table" via hx-swap=outerHTML; a
    non-ok StatusUpdateResult must come back with the error banner inside the
    swapped target, not a bare table that silently reverts the status.
    """
    _patch_reads(monkeypatch, [_report()])

    def fake_update(report_id, *, status, reviewer=None):
        from nctqai.services.compass_reports_client import StatusUpdateResult

        return StatusUpdateResult(
            ok=False, status_code=404, detail="No such report."
        )

    monkeypatch.setattr(route, "update_report_status", fake_update)

    class _Req(_FakeRequest):
        headers = {"HX-Request": "true"}

        async def form(self):
            return {"status": "triaged", "f_status": "", "f_dimension": "", "f_outcome": ""}

    rid = "11111111-1111-1111-1111-111111111111"
    handlers = {}

    def fake_rt(path):
        def deco(fn):
            handlers[path] = fn
            return fn

        return deco

    route.register(fake_rt)
    status_handler = handlers["/compass/quality/reports/{report_id}/status"]
    result = asyncio.run(status_handler(_Req(), rid))
    html = fh.to_xml(result)

    # The backend detail surfaces in the error banner...
    assert "No such report." in html
    assert "quality-reports-banner-error" in html
    # ...and it lands inside the swap target with the refreshed summary strip.
    assert 'id="quality-reports-results"' in html
    assert "Total" in html


def test_reports_status_invalid_uuid_stays_inside_htmx_swap_region(monkeypatch):
    _patch_reads(monkeypatch, [_report()])
    monkeypatch.setattr(
        route,
        "update_report_status",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    class _Req(_FakeRequest):
        headers = {"HX-Request": "true"}

        async def form(self):
            return {"status": "triaged", "f_status": "", "f_dimension": "", "f_outcome": ""}

    handlers = {}

    def fake_rt(path):
        def deco(fn):
            handlers[path] = fn
            return fn

        return deco

    route.register(fake_rt)
    result = asyncio.run(
        handlers["/compass/quality/reports/{report_id}/status"](_Req(), "not-a-uuid")
    )
    html = fh.to_xml(result)
    assert "Invalid report id." in html
    assert 'id="quality-reports-results"' in html
    assert "<html" not in html.lower()

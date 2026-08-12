"""Google Analytics 4 visitor-stats client (read-only), Data API.

Mirrors the public surface of ``umami_stats`` so the Overview tile can read from
GA instead of Umami without any change to the rendering code. Enabled when both
``GA4_PROPERTY_ID`` (the numeric property id) and ``GOOGLE_APPLICATION_CREDENTIALS``
(path to a service-account JSON key) are configured. The service account needs
Viewer access on the property and the Google Analytics Data API enabled.

Unique visitors map to GA's ``activeUsers`` metric. GA dedups by client id across
all time (no monthly-salt reset), so the all-time number is meaningful too.

Dates are sent as GA relative keywords ("today" / "NdaysAgo") so both ends of a
range resolve in the property's own timezone. Sending an absolute UTC date for
one end and "today" for the other can put start after end near midnight.

Results cache 5 minutes per window. The GA client is synchronous, so calls run
in a worker thread.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timezone

from cachetools import TTLCache

from nctqai.services.umami_stats import VisitorsSummary

logger = logging.getLogger(__name__)

# Property id + credentials come from env. Production sets the NCTQAI_ prefixed
# names on the Container App (the key JSON is a secret-backed env var). For local
# dev, the bare GA4_PROPERTY_ID and a GOOGLE_APPLICATION_CREDENTIALS file path
# also work. When nothing is configured, ga_enabled() is False and callers fall
# back to the legacy Umami source.
_PROPERTY = (os.environ.get("NCTQAI_GA4_PROPERTY_ID")
             or os.environ.get("GA4_PROPERTY_ID") or "").strip()

# Restrict the visitor count to a single page. Compass is embedded as a
# cross-site iframe (compass.nctq.ai inside www.nctq.org), so the tag we ship in
# the iframe is a third party there and privacy/tracking protection drops most of
# its hits — it measured ~23 users where the page itself saw ~98 over the same 30
# days. We therefore read NCTQ's own first-party property (which tracks the
# Pathfinder page completely) and filter to the page the iframe sits on. Set to
# an empty string to count the whole property instead.
_PAGE_PATH = os.environ.get(
    "NCTQAI_GA4_PAGE_PATH", "/district-policy-pathfinder/"
).strip()
_CREDS_JSON = os.environ.get("NCTQAI_GA4_CREDENTIALS_JSON", "").strip()
_CREDS_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

# Visitor stats "restart" floor. GA keeps all of its raw data, but the dashboard
# never counts anything before this date, so the visitor tiles read fresh from
# here onward. Set to the 2026-07-15 conversation-data reset so the GA numbers
# stay consistent with the wiped chat history. The GA Data API resolves this
# date in the property's own timezone (NCTQ Compass = America/New_York), so this
# floor is exactly 00:00 Eastern on 2026-07-15. Bump this date for a future clean
# slate. (Mirrors umami_stats._UMAMI_ALLTIME_FLOOR.)
_RESET_FLOOR = date(2026, 7, 15)

_stats_cache: TTLCache = TTLCache(maxsize=32, ttl=300)
_client = None


def ga_enabled() -> bool:
    """True when a GA4 property id and readable credentials are configured."""
    return bool(_PROPERTY and (_CREDS_JSON or (_CREDS_FILE and os.path.isfile(_CREDS_FILE))))


def _get_client():
    global _client
    if _client is None:
        from google.oauth2 import service_account
        from google.analytics.data_v1beta import BetaAnalyticsDataClient

        if _CREDS_JSON:
            import json

            info = json.loads(_CREDS_JSON)
            creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        else:
            creds = service_account.Credentials.from_service_account_file(_CREDS_FILE, scopes=_SCOPES)
        _client = BetaAnalyticsDataClient(credentials=creds)
    return _client


def _floor_days() -> int:
    """Days from the reset floor to today (0 when the floor is today or later)."""
    return max(0, (datetime.now(timezone.utc).date() - _RESET_FLOOR).days)


def _rel(dt: datetime | None) -> str:
    """A GA relative-date keyword for a bound, clamped so it never precedes the
    reset floor. None means all-time, which becomes 'since the reset floor'.

    Also clamps to 'today' when the bound is today or (due to UTC vs property tz)
    a hair into the future, which is what caused start_date > end_date for the
    Today range.
    """
    floor = _floor_days()
    if not isinstance(dt, datetime):
        return "today" if floor <= 0 else f"{floor}daysAgo"
    today = datetime.now(timezone.utc).date()
    days = min((today - dt.date()).days, floor)  # never earlier than the floor
    return "today" if days <= 0 else f"{days}daysAgo"


def _range(start, end):
    return _rel(start), ("today" if end is None else _rel(end))


def _run(start_s: str, end_s: str) -> int:
    from google.analytics.data_v1beta.types import (
        DateRange,
        Filter,
        FilterExpression,
        Metric,
        RunReportRequest,
    )

    kwargs = {}
    if _PAGE_PATH:
        kwargs["dimension_filter"] = FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value=_PAGE_PATH,
                ),
            )
        )
    req = RunReportRequest(
        property=f"properties/{_PROPERTY}",
        date_ranges=[DateRange(start_date=start_s, end_date=end_s)],
        metrics=[Metric(name="activeUsers")],
        **kwargs,
    )
    r = _get_client().run_report(req)
    return int(r.rows[0].metric_values[0].value) if r.rows else 0


async def get_visitors(start: datetime | None, end: datetime | None) -> VisitorsSummary:
    """Unique visitors (GA activeUsers) over the window. Cached 5 minutes."""
    start_s, end_s = _range(start, end)
    key = ("total", start_s, end_s)
    cached = _stats_cache.get(key)
    if cached is not None:
        return cached
    total = await asyncio.to_thread(_run, start_s, end_s)
    summary = VisitorsSummary(total=total)
    _stats_cache[key] = summary
    return summary

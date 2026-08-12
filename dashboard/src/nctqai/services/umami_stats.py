"""Umami analytics client — read-only stats fetcher for /compass/overview.

Self-hosted Umami v2.20.2 doesn't expose user-managed API keys (that's a v3
feature, and the postgresql-v3.x GHCR tag isn't available yet). Auth is via
session JWT obtained from POST /api/auth/login; we cache the JWT for 6 days
(TTL is 7) and refresh once on 401. Stats responses cache 5 minutes so opening
the dashboard repeatedly doesn't hammer Umami.

When the v3 image lands and we get a real API key, this module's public
surface (`get_visitors` / `get_visitors_7d`) stays the same — only `_login`
swaps to a Bearer header backed by the API key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

import httpx
from cachetools import TTLCache

from nctqai.config import Config

logger = logging.getLogger(__name__)

_config = Config()

_jwt_cache: TTLCache = TTLCache(maxsize=1, ttl=6 * 24 * 3600)  # 6-day TTL (Umami JWT is 7d)
# Keyed per window (startAt:endAt), so the 7d / 30d / all-time / custom pills each
# cache independently. maxsize=8 comfortably holds the four presets + a custom
# span + slack without thrashing inside the 5-minute TTL.
_stats_cache: TTLCache = TTLCache(maxsize=8, ttl=300)  # 5-minute TTL

# Lower bound for the "all time" window (since=None). Umami has no traffic before
# the site went live, so a fixed far-back date is a cheap, DB-free floor that
# captures every real visit. Aligned to range_pills._CUSTOM_SINCE_FLOOR
# ("Compass has no data before this") so there's one floor date to reconcile.
# SEAM(#1787): when the launch-floor / clean-slate wipe lands, clamp this to the
# launch date so pre-launch Umami traffic doesn't leak through while the DB-side
# metrics are clean.
_UMAMI_ALLTIME_FLOOR = datetime(2025, 1, 1, tzinfo=UTC)

_MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class VisitorsSummary:
    """Unique visitors over a window. `new`/`returning` left None in v1 — the
    Umami v2 /stats endpoint doesn't return that split in a single call."""

    total: int
    new: int | None = None
    returning: int | None = None


async def _login(client: httpx.AsyncClient) -> str:
    cached = _jwt_cache.get("token")
    if cached:
        return cached
    r = await client.post(
        f"{_config.umami_base_url}/api/auth/login",
        json={
            "username": _config.umami_username,
            "password": _config.umami_password.get_secret_value(),
        },
    )
    r.raise_for_status()
    token = r.json()["token"]
    _jwt_cache["token"] = token
    return token


def umami_enabled() -> bool:
    """True when Umami credentials are configured (the tile can fetch data).

    Lets callers skip ``get_visitors_7d`` entirely in envs without creds so the
    expected "not configured" path degrades quietly instead of logging an error
    traceback on every page load.
    """
    return _config.umami_enabled


async def get_visitors(
    start: datetime | None, end: datetime | None
) -> VisitorsSummary:
    """Total unique visitors over an arbitrary window. Cached 5 minutes per window.

    ``start``/``end`` are the Overview route's resolved ``since``/``until`` bounds
    (the same ones every other card uses), mapped to Umami's ``startAt``/``endAt``
    millisecond-epoch params:

    - ``start is None`` → the "all time" window: floors to ``_UMAMI_ALLTIME_FLOOR``.
    - ``end is None``   → "up to now": uses the current time.

    Raises RuntimeError if Umami credentials aren't configured. Network/HTTP
    errors propagate — callers should wrap in try/except for graceful UI degrade.
    """
    if not _config.umami_enabled:
        raise RuntimeError("Umami not configured (NCTQAI_UMAMI_USERNAME/PASSWORD missing)")

    start_dt = start if start is not None else _UMAMI_ALLTIME_FLOOR
    end_dt = end if end is not None else datetime.now(UTC)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # Cache per UTC-day bucket, not per-millisecond. The rolling presets (7d/30d)
    # recompute now() on every request, so keying on the exact ms would mint a
    # fresh key each load and the 5-minute TTL would never hit — re-hitting Umami
    # on every page view. Day granularity is immaterial for a visitor count
    # inside a 5-minute window and lets repeated loads of the same window share
    # one fetch. The fetch itself still uses the precise bounds.
    cache_key = f"visitors:{start_ms // _MS_PER_DAY}:{end_ms // _MS_PER_DAY}"
    cached = _stats_cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"{_config.umami_base_url}/api/websites/{_config.umami_website_id}/stats"
    params = {"startAt": start_ms, "endAt": end_ms}

    async with httpx.AsyncClient(timeout=5.0) as client:
        token = await _login(client)
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        if r.status_code == 401:
            _jwt_cache.pop("token", None)
            token = await _login(client)
            r = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params
            )
        r.raise_for_status()

    data = r.json()
    summary = VisitorsSummary(total=int(data["visitors"]["value"]))
    _stats_cache[cache_key] = summary
    return summary


async def get_visitors_7d() -> VisitorsSummary:
    """Backwards-compatible wrapper: total unique visitors over the last 7 days.

    Thin shim over ``get_visitors`` so any caller still on the fixed-7d contract
    keeps working; new callers should pass an explicit window to ``get_visitors``.
    """
    end = datetime.now(UTC)
    return await get_visitors(end - timedelta(days=7), end)

"""Eastern Time display helpers for the Compass dashboard."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")


def eastern_time(timestamp: datetime | None) -> datetime | None:
    """Treat naive stored timestamps as UTC and return Eastern Time."""
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(_EASTERN)


def format_eastern_timestamp(timestamp: datetime | None) -> str:
    """Format a full dashboard timestamp with an explicit ET label."""
    local = eastern_time(timestamp)
    return local.strftime("%b %-d, %Y, %-I:%M %p ET") if local else ""


def format_relative_eastern_timestamp(
    timestamp: datetime | None, *, now: datetime | None = None
) -> str:
    """Use a compact but timezone-explicit label for the conversation list."""
    local = eastern_time(timestamp)
    if local is None:
        return ""
    local_now = eastern_time(now or datetime.now(UTC))
    assert local_now is not None
    seconds = (local_now - local).total_seconds()
    time_text = local.strftime("%-I:%M %p ET")
    if seconds < 86400 and local_now.date() == local.date():
        return time_text
    if seconds < 604800:
        return local.strftime("%a ") + time_text
    return local.strftime("%b %-d, ") + time_text

"""Shared utility functions for the NCTQ.ai dashboard."""

# Academic year options available across the dashboard
AY_OPTIONS: list[int] = [25, 24, 23]
DEFAULT_AY: int = 25


def format_ay(ay_id: int) -> str:
    """Format academic year ID as display string: 25 -> '2024-2025'."""
    return f"20{ay_id - 1:02d}-20{ay_id:02d}"


def format_ay_ids(ay_ids: list[int] | None) -> str:
    """Format list of AY IDs as display string: [25, 26] -> '2024-2025, 2025-2026'."""
    if not ay_ids:
        return "\u2014"
    return ", ".join(format_ay(a) for a in sorted(ay_ids))

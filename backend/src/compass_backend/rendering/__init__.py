"""Rendering layer for fresh Compass responses."""

from .writer import (
    attach_adjacent_metrics_manifest_metadata,
    display_metadata_for_result,
    render_response,
)

__all__ = [
    "attach_adjacent_metrics_manifest_metadata",
    "display_metadata_for_result",
    "render_response",
]

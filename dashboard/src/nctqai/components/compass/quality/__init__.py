"""Quality dashboard UI components for the Compass Quality Scorecard.

Re-exports public components for convenient import:

    from nctqai.components.compass.quality import DimensionRow, K3Strip, VerdictList
"""
from nctqai.components.compass.quality.chrome import (
    breadcrumb_strip,
    build_context_strip,
    last_sweep_strip,
    latest_finished_across,
)
from nctqai.components.compass.quality.k3_strip import K3Strip
from nctqai.components.compass.quality.scorecard_row import DimensionRow
from nctqai.components.compass.quality.verdict_list import VerdictList

__all__ = [
    "breadcrumb_strip",
    "build_context_strip",
    "last_sweep_strip",
    "latest_finished_across",
    "DimensionRow",
    "K3Strip",
    "VerdictList",
]

"""Committed scorecard threshold contract for runtime and pa-eval reads."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from compass_backend.quality.scorecard_models import CANONICAL_DIM_SLUGS

DEFAULT_THRESHOLD_PATH = Path(__file__).with_name("scorecard_thresholds.yaml")
ThresholdStatus = Literal["pass", "fail", "no_data"]


class ReproducibilityBand(BaseModel):
    """Tolerance band for the K-sweep reproducibility acceptance gate (#1259).

    LLM calls make bit-identical sweeps impossible, so "reproducible" is a
    tolerance band over N completed sweep runs of the same dimension rather
    than an equality check. The three sub-gates:

    - Primary (dimension ``score_pct``): max pairwise |Δscore_pct| within
      ``max_pairwise_delta_pct`` AND sample stdev within ``max_stdev_pct``.
    - Secondary (per-case ``pass_rate`` stability): no case's pass_rate swings
      by more than ``max_case_pass_rate_swing`` (a fraction, 0..1) between any
      two runs, and no case crosses the 0.5 pass/fail boundary in more than
      ``max_boundary_crossing_runs`` of the runs.
    - Tertiary (deterministic buckets): the skip / error / stub-error counts
      must be identical across runs — enforced with zero tolerance, no knob.

    Tighten these once empirical variance is measured (per the sprint plan).
    """

    max_pairwise_delta_pct: float = Field(default=5.0, ge=0)
    max_stdev_pct: float = Field(default=3.0, ge=0)
    max_case_pass_rate_swing: float = Field(default=1.0 / 3.0, ge=0, le=1)
    max_boundary_crossing_runs: int = Field(default=1, ge=0)


#: Per-dimension reproducibility band, keyed by canonical dim slug. A dimension
#: absent from this map falls back to :data:`DEFAULT_REPRODUCIBILITY_BAND`.
#: Empirical-variance tightening lands here (and only here) so the gate stays
#: tunable without touching the check logic.
DEFAULT_REPRODUCIBILITY_BAND = ReproducibilityBand()
REPRODUCIBILITY_BANDS: dict[str, ReproducibilityBand] = {
    slug: ReproducibilityBand() for slug in CANONICAL_DIM_SLUGS
}


def reproducibility_band_for_slug(slug: str) -> ReproducibilityBand:
    """Return the reproducibility band for ``slug``, or the default band."""
    return REPRODUCIBILITY_BANDS.get(slug, DEFAULT_REPRODUCIBILITY_BAND)


class DimensionThreshold(BaseModel):
    launch_threshold_pct: int = Field(ge=0, le=100)


class ThresholdConfig(BaseModel):
    version: str
    review_date: str
    dimensions: dict[str, DimensionThreshold]

    @model_validator(mode="after")
    def _canonical_dimensions(self) -> "ThresholdConfig":
        expected = list(CANONICAL_DIM_SLUGS)
        actual = list(self.dimensions)
        if set(actual) != set(expected):
            raise ValueError(
                "threshold dimensions must match canonical scorecard slugs "
                f"in order; expected={expected!r} actual={actual!r}"
            )
        self.dimensions = {slug: self.dimensions[slug] for slug in expected}
        return self


@lru_cache(maxsize=8)
def _load_threshold_config_cached(path_str: str) -> ThresholdConfig:
    raw = yaml.safe_load(Path(path_str).read_text()) or {}
    return ThresholdConfig.model_validate(raw)


def load_threshold_config(path: Path | None = None) -> ThresholdConfig:
    resolved = path or DEFAULT_THRESHOLD_PATH
    return _load_threshold_config_cached(str(resolved))


def threshold_for_slug(
    slug: str,
    config: ThresholdConfig | None = None,
) -> int | None:
    resolved = config or load_threshold_config()
    threshold = resolved.dimensions.get(slug)
    return threshold.launch_threshold_pct if threshold is not None else None


def threshold_status(
    *,
    score_pct: int,
    n_trials: int,
    threshold_pct: int | None,
) -> ThresholdStatus:
    if n_trials == 0 or threshold_pct is None:
        return "no_data"
    return "pass" if score_pct >= threshold_pct else "fail"


__all__ = [
    "DEFAULT_THRESHOLD_PATH",
    "DEFAULT_REPRODUCIBILITY_BAND",
    "REPRODUCIBILITY_BANDS",
    "DimensionThreshold",
    "ReproducibilityBand",
    "ThresholdConfig",
    "ThresholdStatus",
    "load_threshold_config",
    "reproducibility_band_for_slug",
    "threshold_for_slug",
    "threshold_status",
]

"""Strict Milestone 1 closure-gate runner.

This wrapper pins the #782 escape-hatch retirement evidence set and refuses to
run unless planning recognition is in finalize mode. The default Compass runtime
is finalize; explicit non-finalize overrides fail this closure gate. The generic
fresh scenario gate stays flexible; this module is intentionally opinionated for
M1 closure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping

from compass_backend.testing.fresh_scenario_gate import (
    ScenarioGateCaseResult,
    render_gate_results,
    run_fresh_scenario_cases,
)


M1_CLOSURE_CASE_IDS: tuple[int, ...] = (
    365,  # strike legality no-position/follow-up
    392,  # observation natural-language phrasing
    436,  # health-premium ranking
    955,  # sick-leave peer comparison/non-covered anchor
    1006,  # Golden 24 starting salary + workdays
    1009,  # FRPL salary rank
    1012,  # meaningful benefits
    1024,  # MA FRPL sort
    323,  # districts-like-mine peer selection
)


class M1ClosureGateModeError(RuntimeError):
    """Raised when the M1 closure gate is not running in finalize mode."""


def require_planning_recognition_finalize(
    env: Mapping[str, str] | None = None,
) -> None:
    """Fail unless planning recognition resolves to finalize mode.

    #1248 collapsed the planner to a single clean path: there is no longer a
    ``planning_recognition_mode`` setting and the always-on finalize verifier
    is the only path. The legacy ``COMPASS_PLANNING_RECOGNITION_MODE`` env knob
    is gone, so an unset env (the only valid state now) is finalize. A stale
    non-finalize override still fails loudly so old runner configs surface.
    """

    values = env or os.environ
    configured = values.get("COMPASS_PLANNING_RECOGNITION_MODE")
    mode = configured.strip().casefold() if configured is not None else "finalize"
    if mode != "finalize":
        raise M1ClosureGateModeError(
            "M1 closure gate requires "
            "planning recognition finalize mode."
        )


async def run_m1_closure_gate(
    *,
    api_url: str,
    api_token: str | None = None,
    concurrency: int = 1,
    require_finalize: bool = True,
) -> list[ScenarioGateCaseResult]:
    """Run the pinned M1 case set through the active fresh scenario gate."""

    if require_finalize:
        require_planning_recognition_finalize()
    return await run_fresh_scenario_cases(
        case_ids=list(M1_CLOSURE_CASE_IDS),
        concurrency=concurrency,
        api_url=api_url,
        api_token=api_token,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument("--api-token", default=os.getenv("COMPASS_API_TOKEN"))
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--output", choices=["markdown", "json"], default="markdown")
    parser.add_argument(
        "--allow-non-finalize",
        action="store_true",
        help="Dry-run only: do not require COMPASS_PLANNING_RECOGNITION_MODE=finalize.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        results = asyncio.run(
            run_m1_closure_gate(
                api_url=args.api_url,
                api_token=args.api_token,
                concurrency=args.concurrency,
                require_finalize=not args.allow_non_finalize,
            )
        )
    except M1ClosureGateModeError as exc:
        print(f"[m1-closure-gate] FATAL: {exc}")
        return 1

    print(render_gate_results(results, output=args.output))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

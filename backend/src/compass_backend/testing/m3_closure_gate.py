"""Strict Milestone 3 closure-gate runner.

This wrapper pins the M3 multi-turn follow-up evidence set and refuses to run
unless planning recognition is in finalize mode. The default Compass runtime
is finalize; explicit non-finalize overrides fail this closure gate. The
generic fresh scenario gate stays flexible; this module is intentionally
opinionated for M3 closure.

Pinned cases trace to:
- src/compass_data_sync/migrations/045_seed_multi_turn_followup_scenarios.sql
  (multi-turn follow-up scenarios MULTI-TURN-2026-05-FOLLOWUP-001..006;
   M3a/M3b/M3c-1; #731/#732/#733)
- src/compass_data_sync/migrations/056_m3_do_all_n_pending_context.sql
  (MULTI-TURN-2026-05-FOLLOWUP-007; M3c-2 "do all N separately"; #769)
- src/compass_data_sync/migrations/063_m3_composite_ranking_executable.sql
  (MULTI-TURN-2026-05-FOLLOWUP-008; M3c-2 executable composite emit;
   #806/#852/#858/#860/#861)
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping

from compass_backend.testing.fresh_scenario_gate import (
    ScenarioGateCaseResult,
    render_gate_results,
    run_fresh_scenario_cases_by_code,
)


M3_CLOSURE_CASE_CODES: tuple[str, ...] = (
    # 045 - multi-turn follow-up regressions (#731/#732/#733)
    "MULTI-TURN-2026-05-FOLLOWUP-001-C00",  # M3a-5: count -> list + new attribute
    "MULTI-TURN-2026-05-FOLLOWUP-002-C00",  # M3a-2: count -> metric pick -> sort
    "MULTI-TURN-2026-05-FOLLOWUP-003-C00",  # M3b-1: named districts + MA refinement
    "MULTI-TURN-2026-05-FOLLOWUP-004-C00",  # M3b-6: peer set + outside-state refine
    "MULTI-TURN-2026-05-FOLLOWUP-005-C00",  # M3b-7: peer set + enrollment-only swap
    "MULTI-TURN-2026-05-FOLLOWUP-006-C00",  # M3c-1: multi-metric single-table BA+MA
    # 056 - "do all N separately" pending context (#769/#733)
    "MULTI-TURN-2026-05-FOLLOWUP-007-C00",  # M3c-2: PendingQueryContext keeps all N
    # 063 - "do all N separately" executable composite (#806/#852/#858/#860/#861)
    "MULTI-TURN-2026-05-FOLLOWUP-008-C00",  # M3c-2: executor + renderer emit composite
)


class M3ClosureGateModeError(RuntimeError):
    """Raised when the M3 closure gate is not running in finalize mode."""


def require_planning_recognition_finalize(
    env: Mapping[str, str] | None = None,
) -> None:
    """Fail unless planning recognition resolves to finalize mode.

    #1248 collapsed the planner to a single clean path: there is no longer a
    ``planning_recognition_mode`` setting and the always-on finalize verifier
    is the only path. An unset ``COMPASS_PLANNING_RECOGNITION_MODE`` env (the
    only valid state now) is finalize; a stale non-finalize override still
    fails loudly so old runner configs surface.
    """

    values = env or os.environ
    configured = values.get("COMPASS_PLANNING_RECOGNITION_MODE")
    mode = configured.strip().casefold() if configured is not None else "finalize"
    if mode != "finalize":
        raise M3ClosureGateModeError(
            "M3 closure gate requires "
            "planning recognition finalize mode."
        )


async def run_m3_closure_gate(
    *,
    api_url: str,
    api_token: str | None = None,
    concurrency: int = 1,
    require_finalize: bool = True,
) -> list[ScenarioGateCaseResult]:
    """Run the pinned M3 case set through the active fresh scenario gate."""

    if require_finalize:
        require_planning_recognition_finalize()
    return await run_fresh_scenario_cases_by_code(
        case_codes=list(M3_CLOSURE_CASE_CODES),
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
            run_m3_closure_gate(
                api_url=args.api_url,
                api_token=args.api_token,
                concurrency=args.concurrency,
                require_finalize=not args.allow_non_finalize,
            )
        )
    except M3ClosureGateModeError as exc:
        print(f"[m3-closure-gate] FATAL: {exc}")
        return 1

    print(render_gate_results(results, output=args.output))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

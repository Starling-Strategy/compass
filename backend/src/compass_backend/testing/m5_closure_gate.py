"""Strict Milestone 5 closure-gate runner.

This wrapper pins the M5 citations-and-exports evidence set and refuses
to run unless planning recognition is in finalize mode. The default
Compass runtime is finalize; explicit non-finalize overrides fail this
closure gate. The generic fresh scenario gate stays flexible; this
module is intentionally opinionated for M5 closure.

Pinned cases trace to:

- src/compass_data_sync/migrations/065_w2_m5_01_csv_parity_regression.sql
  (REGR-W2-M5-01-COMPOSITE-CSV-NOT-BLANK; W2-M5-01 CSV parity; #745)
- src/compass_data_sync/migrations/069_w2_m5_02_citation_title_regression.sql
  (REGR-W2-M5-02-CITATION-TITLE-RESOLVED; W2-M5-02 citation titles; #746)
- src/compass_data_sync/migrations/067_w2_m5_03_nces_citation_regression.sql
  (REGR-W2-M5-03-FRPL-LOOKUP-CITATION; W2-M5-03 NCES profile-lookup; #747)
- src/compass_data_sync/migrations/062_w0_01_enrollment_ranking_regression.sql
  (REGR-W0-01-PROFILE-RANK-PROOF; W2-M5-03 NCES profile-rank; #747)
- src/compass_data_sync/migrations/071_w2_m5_04_sources_dedup_regression.sql
  (REGR-W2-M5-04-SOURCES-DEDUP; W2-M5-04 sources-block dedup; #748)

Required by closure plan §13 Wave 2 exit criterion #4 — one pinned
case_id per M5 issue, evaluated end-to-end through the live verdict
pipeline.
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


M5_CLOSURE_CASE_CODES: tuple[str, ...] = (
    # 065 - composite CSV parity (#745)
    "REGR-W2-M5-01-COMPOSITE-CSV-NOT-BLANK-C00",
    # 069 - citation-title fallback (#746)
    "REGR-W2-M5-02-CITATION-TITLE-RESOLVED-C00",
    # 067 - profile-lookup NCES citation (#747)
    "REGR-W2-M5-03-FRPL-LOOKUP-CITATION-C00",
    # 062 - profile-rank NCES citation (#747 — second profile path)
    "REGR-W0-01-PROFILE-RANK-PROOF-C00",
    # 071 - composite Sources-block dedup (#748)
    "REGR-W2-M5-04-SOURCES-DEDUP-C00",
)


class M5ClosureGateModeError(RuntimeError):
    """Raised when the M5 closure gate is not running in finalize mode."""


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
        raise M5ClosureGateModeError(
            "M5 closure gate requires "
            "planning recognition finalize mode."
        )


async def run_m5_closure_gate(
    *,
    api_url: str,
    api_token: str | None = None,
    concurrency: int = 1,
    require_finalize: bool = True,
) -> list[ScenarioGateCaseResult]:
    """Run the pinned M5 case set through the active fresh scenario gate."""

    if require_finalize:
        require_planning_recognition_finalize()
    return await run_fresh_scenario_cases_by_code(
        case_codes=list(M5_CLOSURE_CASE_CODES),
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
            run_m5_closure_gate(
                api_url=args.api_url,
                api_token=args.api_token,
                concurrency=args.concurrency,
                require_finalize=not args.allow_non_finalize,
            )
        )
    except M5ClosureGateModeError as exc:
        print(f"[m5-closure-gate] FATAL: {exc}")
        return 1

    print(render_gate_results(results, output=args.output))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

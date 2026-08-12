"""Active fresh Compass scenario-gate runner.

SMOKE-ONLY: this CLI replays a flexible slice of active B-spine cases against a
running Compass API. It is *not* a milestone-closure gate -- it does not pin a
case set or enforce ``COMPASS_PLANNING_RECOGNITION_MODE=finalize``. Strict
milestone-closure gates live alongside in
``compass_backend.testing.m1_closure_gate``,
``compass_backend.testing.m2_closure_gate``,
``compass_backend.testing.m3_closure_gate``, and
``compass_backend.testing.m5_closure_gate``; those modules pin a tuple of case
codes and refuse to run outside finalize mode.

This module intentionally lives under ``src/compass_backend/testing`` so new
validation does not depend on archived v3 sweep scripts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from compass_backend.config import settings as runtime_settings
from compass_backend.db import ReadOnlyScenarioCaseRepository
from compass_backend.db._pool import close_chat_pool, create_chat_pool


OutputFormat = Literal["json", "markdown"]


from contextlib import asynccontextmanager


@asynccontextmanager
async def _scenario_repository_pool():
    """Yield a short-lived asyncpg pool for one CLI scenario-gate invocation.

    Each entry point creates a small pool, uses it to read the active
    B-spine cases, and closes it on exit. The pool lives just long enough
    to satisfy the chat-pool requirement on ``ReadOnlyScenarioCaseRepository``.
    """

    pool = await create_chat_pool(runtime_settings)
    try:
        yield pool
    finally:
        await close_chat_pool(pool, timeout=10.0)


class ScenarioGateTurnResult(BaseModel):
    """One chat turn executed by the scenario gate."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    status_code: int | None = None
    response_text: str = ""
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.status_code != 200 or self.error:
            return False
        lowered = self.response_text.casefold()
        return not any(
            marker in lowered
            for marker in (
                "i could not structure that request safely",
                "i couldn't safely run that request as structured",
                "i could not resolve",
                "deterministic execution does not support",
            )
        )


class ScenarioGateCaseResult(BaseModel):
    """One runnable B-spine case result."""

    model_config = ConfigDict(extra="forbid")

    case_id: int
    scenario_id: int
    title: str
    feature: str | None = None
    turns: list[ScenarioGateTurnResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.turns) and all(turn.passed for turn in self.turns)


async def run_fresh_scenario_gate(
    *,
    feature: str | None = None,
    scenario_ids: list[int] | None = None,
    title_contains: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    concurrency: int = 4,
    api_url: str = "http://127.0.0.1:8010",
    api_token: str | None = None,
) -> list[ScenarioGateCaseResult]:
    """Load active B-spine cases and replay them against a fresh Compass API."""

    async with _scenario_repository_pool() as pool:
        repository = ReadOnlyScenarioCaseRepository(pool=pool)
        cases = await repository.list_cases(feature=feature)
    if scenario_ids:
        selected_ids = set(scenario_ids)
        cases = [
            case
            for case in cases
            if case.scenario_id in selected_ids or case.case_id in selected_ids
        ]
    if title_contains:
        needle = title_contains.casefold()
        cases = [
            case
            for case in cases
            if needle in case.scenario_title.casefold()
            or needle in case.initial_user_message.casefold()
        ]
    if offset:
        cases = cases[offset:]
    if limit is not None:
        cases = cases[:limit]

    semaphore = asyncio.Semaphore(max(1, concurrency))
    headers = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        return await asyncio.gather(
            *[
                _run_case(client, case, api_url=api_url, semaphore=semaphore)
                for case in cases
            ]
        )


async def run_fresh_scenario_cases(
    *,
    case_ids: list[int],
    concurrency: int = 4,
    api_url: str = "http://127.0.0.1:8010",
    api_token: str | None = None,
) -> list[ScenarioGateCaseResult]:
    """Replay exact active B-spine case ids against a fresh Compass API."""

    async with _scenario_repository_pool() as pool:
        repository = ReadOnlyScenarioCaseRepository(pool=pool)
        cases = []
        for case_id in case_ids:
            case = await repository.get_case(case_id)
            if case is None:
                raise ValueError(f"fresh scenario case not found: {case_id}")
            cases.append(case)

    semaphore = asyncio.Semaphore(max(1, concurrency))
    headers = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        return await asyncio.gather(
            *[
                _run_case(client, case, api_url=api_url, semaphore=semaphore)
                for case in cases
            ]
        )


async def run_fresh_scenario_cases_by_code(
    *,
    case_codes: list[str],
    concurrency: int = 4,
    api_url: str = "http://127.0.0.1:8010",
    api_token: str | None = None,
) -> list[ScenarioGateCaseResult]:
    """Replay active B-spine cases identified by ``case_code`` strings.

    ``case_code`` is the stable, deterministic identifier seeded by the
    migrations (the integer ``id`` column is auto-assigned per environment), so
    milestone-closure gates pin codes rather than ids.
    """

    async with _scenario_repository_pool() as pool:
        repository = ReadOnlyScenarioCaseRepository(pool=pool)
        inventory = await repository.list_cases()
    by_code = {case.case_code: case for case in inventory if case.case_code}
    cases = []
    for case_code in case_codes:
        case = by_code.get(case_code)
        if case is None:
            raise ValueError(f"fresh scenario case not found: {case_code!r}")
        cases.append(case)

    semaphore = asyncio.Semaphore(max(1, concurrency))
    headers = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        return await asyncio.gather(
            *[
                _run_case(client, case, api_url=api_url, semaphore=semaphore)
                for case in cases
            ]
        )


async def _run_case(
    client: httpx.AsyncClient,
    case,
    *,
    api_url: str,
    semaphore: asyncio.Semaphore,
) -> ScenarioGateCaseResult:
    async with semaphore:
        prompts = [step.prompt for step in case.input_steps]
        if not prompts:
            prompts = [case.initial_user_message]
        turns: list[ScenarioGateTurnResult] = []
        session_id: str | None = None
        for prompt in prompts:
            payload: dict[str, object] = {"message": prompt}
            if session_id:
                payload["session_id"] = session_id
            try:
                response = await client.post(
                    f"{api_url.rstrip('/')}/api/v1/chat/simple",
                    json=payload,
                )
                body = response.json()
                session_id = (
                    body.get("session", {}).get("session_id")
                    if isinstance(body, dict)
                    else session_id
                )
                response_text = _response_text(body)
                turns.append(
                    ScenarioGateTurnResult(
                        prompt=prompt,
                        status_code=response.status_code,
                        response_text=response_text,
                    )
                )
            except Exception as exc:  # pragma: no cover - network failure surface
                turns.append(
                    ScenarioGateTurnResult(
                        prompt=prompt,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                break
        return ScenarioGateCaseResult(
            case_id=case.case_id,
            scenario_id=case.scenario_id,
            title=case.scenario_title,
            feature=case.feature,
            turns=turns,
        )


def _response_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("assistant_message", "message", "answer"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    turn = body.get("turn")
    if isinstance(turn, dict):
        value = turn.get("assistant_message") or turn.get("message")
        if isinstance(value, str):
            return value
    return json.dumps(body, ensure_ascii=True)[:1200]


def render_gate_results(
    results: list[ScenarioGateCaseResult],
    *,
    output: OutputFormat,
) -> str:
    if output == "json":
        return json.dumps(
            [result.model_dump(mode="json") for result in results],
            ensure_ascii=True,
            indent=2,
        )

    passed = sum(1 for result in results if result.passed)
    lines = [
        "# Fresh Compass Scenario Gate",
        "",
        f"Passed {passed}/{len(results)} cases.",
        "",
        "| Case | Scenario | Status | Title |",
        "|---:|---:|---|---|",
    ]
    for result in results:
        status = "pass" if result.passed else "fail"
        lines.append(
            f"| {result.case_id} | {result.scenario_id} | {status} | "
            f"{result.title} |"
        )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature")
    parser.add_argument("--scenario-ids")
    parser.add_argument("--title-contains")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--api-url", default="http://127.0.0.1:8010")
    parser.add_argument("--api-token", default=os.getenv("COMPASS_API_TOKEN"))
    return parser


def _parse_ids(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    return [int(value.strip()) for value in raw.split(",") if value.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    results = asyncio.run(
        run_fresh_scenario_gate(
            feature=args.feature,
            scenario_ids=_parse_ids(args.scenario_ids),
            title_contains=args.title_contains,
            limit=args.limit,
            offset=args.offset,
            concurrency=args.concurrency,
            api_url=args.api_url,
            api_token=args.api_token,
        )
    )
    print(render_gate_results(results, output=args.output))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

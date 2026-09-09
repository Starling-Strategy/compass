# Backend engineering guide

Applies to this backend application, including its manifest, image, scripts,
content, and tests. Follow the [repository guide](../AGENTS.md) for shared
workflow, security, and approval rules. Before changing runtime Python, read
[the package guardrails](src/compass_backend/AGENTS.md); prompt changes also
follow [the prompt-asset guide](src/compass_backend/instructions/AGENTS.md).
These are engineering instructions, not runtime model prompts.

## Setup and checks

Run these commands from `backend/` with Python 3.12 and uv:

```bash
uv sync --frozen --no-install-project
PYTHONPATH=src uv run --no-sync uvicorn compass_backend.main:app --port 8000
```

Startup requires approved runtime configuration; installing dependencies does
not provide database access or model credentials. Settings are authoritative in
[`config.py`](src/compass_backend/config.py), and model configuration in
[`agents/model_settings.py`](src/compass_backend/agents/model_settings.py).

For a focused health-route test:

```bash
PYTHONPATH=src uv run --no-sync pytest src/compass_backend/tests/test_api_health.py
```

For the backend suite:

```bash
PYTHONPATH=src uv run --no-sync pytest src/compass_backend/tests
```

Pass explicit test paths. The inherited pytest configuration names additional
suites absent from this repository; its default markers exclude `slow` and
`llm`. Inspect the selected tests before running them: default marker exclusion
is not a guarantee of offline execution. Live/model tests and scenario replays
require approved access and authorization. Report collection failures, missing
tools, and unrun checks rather than treating them as passes.

[`pyproject.toml`](pyproject.toml) and [`uv.lock`](uv.lock) are the dependency
authorities. Use source-first execution: the inherited packaging configuration
and CLI entries also reference packages and evaluation scripts not shipped here.
Do not recreate those packages or rewrite the manifest merely to run a check.
Ruff configuration targets Python 3.12, 100-character lines, and `F` rules;
Ruff itself is not declared in the backend dependency groups.

## Application boundaries

- `src/compass_backend/`: FastAPI runtime, typed contracts, planning, execution,
  rendering, quality, and database adapters.
- `src/compass_backend/instructions/`: runtime prompt assets and their specialized
  authoring rules; keep product logic and live data facts in Python.
- `content/`, `static/`: application content and bundled assets.
- `Dockerfile`, `scripts/entrypoint.sh`: local image and startup definitions.

Bug fixes need focused regression tests. User-visible planner or answer changes
also need approved scenario replay and scorecard evidence, following
[Quality & Evaluation](../docs/04-quality-and-evaluation.md). Missing replay
tooling is a blocker to report, not permission to drop the evidence requirement.

A local image build, from `backend/`, is:

```bash
docker build -t compass-api .
```

This is not deployment or live acceptance. Docker health checks liveness;
readiness includes a database probe. Azure DevOps preserves its own deployment
scaffolding, including `Dockerfile.api`; use the authoritative
[deployment runbook](../docs/06-hosting-deployment-security.md) for that boundary.
Changes inside `backend/`, including documentation and tests, can trigger its
sync/deployment path after an approved merge.

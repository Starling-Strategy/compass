# Compass Backend

Active workspace for the unified Compass backend.

Staging now runs this `src/compass_backend/` package. The former legacy, classic, v3, and frozen v2 Compass packages have been deleted from disk; retrieve historical reference via `git show <sha>:src/archive/<path>`.

This folder is intentionally named `compass_backend` to separate the active
backend from the archived legacy `compass_api` package.

Run active backend checks with `PYTHONPATH=src`:

```bash
PYTHONPATH=src uv run pytest src/compass_backend/tests/test_mvp_chat.py
```

Fresh workbench container checks use the separate Dockerfile so current staging
deployment remains untouched:

```bash
docker build -f Dockerfile.compass_api_fresh -t compass-api-fresh .
docker run --rm -p 8010:8000 \
  -e PYTHONPATH=/app/src \
  -e PG_HOST=<private-db-host> \
  -e PG_PORT=5435 \
  -e PG_DATABASE=nctq \
  -e PG_USER=nctq_user \
  -e PG_PASSWORD='<staging-password>' \
  -e PG_SCHEMA=compass \
  compass-api-fresh
curl http://localhost:8010/api/v1/health
```

The fresh health route runs a read-only database check against
`PG_SCHEMA.navigator_metrics`. A healthy container should return
`"database":"connected"` when the staging Compass schema is reachable and
`"database":"unavailable"` when credentials, network access, or the schema
contract are missing. The fresh Docker `HEALTHCHECK` treats only
`"database":"connected"` as container-ready.

Intended layers:

- `api` - FastAPI app setup, routes, SSE, auth boundaries, middleware.
- `db` - database access and persistence adapters.
- `session` - `SessionState`, `TurnSnapshot`, and turn lifecycle state.
- `planning` - Pydantic AI planner and route selection.
- `prompts` - reviewable static model instructions and planner guidance loaded
  as packaged markdown assets.
- `contracts` - typed planner turns, query plans, specs, manifests.
- `execution` - deterministic selection, filtering, sorting, limits, fetches.
- `artifacts` - `ResultSet`, `EvidenceMap`, `AnswerArtifact`, source metadata.
- `rendering` - writer inputs, markdown, tables, charts, and export shaping.
- `agents` - agent wrappers that sit behind typed contracts.
- `quality` - Accuracy Framework validators and risk checks.
- `legacy` - temporary compatibility code during migration.

Do not add runtime files here without a matching row in the Compass intake
ledger.

Planner context follows the Pydantic AI dependency model. The raw user message
is passed as the agent run prompt, while prior `SessionState`, `QueryContext`,
pending clarification slots, transcript snippets, and safe runtime hints are
passed as typed planner deps. Model-visible context is rendered from those deps
through dynamic Pydantic AI instructions. The chat router owns one lazy cached
planner agent and supplies fresh typed deps on each turn. Provider message
history is persisted as planner evidence for replay/debugging, not as
deterministic execution memory.

Static model instructions and planner snippet bodies live in
`src/compass_backend/instructions/`. See
../../docs/architecture/compass-prompt-and-prose-guidance.md (not vendored in this snapshot)
for the ownership rules that separate prompts, Pydantic field descriptions,
renderer copy, NCTQ policy content, and model routing.

# AGENTS.md

## Project and code map

Compass is NCTQ's district-policy research assistant. This GitHub repository is
the application-source authority; Azure DevOps application copies are deployment
projections, not places to develop features. See [README.md](README.md) for the
document map and [PROVENANCE.md](PROVENANCE.md) for historical import provenance.

- `backend/src/compass_backend/`: Python/FastAPI API; typed contracts, planning,
  execution, rendering, quality, and database adapters. Runtime model prompts live
  in `instructions/`, not in this engineering guide.
- `frontend/`: PHP/Apache public interface and server-side API proxy;
  `public/assets/js/` contains browser modules, `src/input.css` is Tailwind input.
- `dashboard/src/nctqai/`: Python/FastHTML staff UI. Metric Calculator writes
  validated metrics; Compass observability reads Compass data.
- `docs/`: maintained product and operational documentation; `docs/research/`
  contains historical research. `resources/` holds design reference material.
- `.github/workflows/sync-to-azure-devops.yml`: authoritative sync/queue contract.

Read the nearest nested `AGENTS.md` before changing a component. Some inherited
component notes reference tooling and paths from the pre-curation repository;
verify those exist here rather than recreating missing packages or assuming a
slash command is installed.

## Setup, build, and tests

Run each block from the stated directory. Use Python 3.12, uv, Node 20/npm,
PHP 8.3/Composer, and Docker as appropriate to the checked-in Dockerfiles.
Runtime startup requires approved environment configuration; dependency setup
alone does not provide a database, model credentials, or authenticated access.

Backend, from `backend/` (source-first, without installing legacy entry points):

```bash
uv sync --frozen --no-install-project
PYTHONPATH=src uv run --no-sync uvicorn compass_backend.main:app --port 8000
PYTHONPATH=src uv run --no-sync pytest src/compass_backend/tests/test_api_health.py
```

Use explicit test paths: the inherited pytest configuration also names suites
not included in this curated repository. Its default markers exclude `slow` and
`llm`; live/model tests require separate authorization and credentials.

Frontend, from `frontend/`:

```bash
npm ci
composer install
npm run build-css
npm test
npm run dev
```

`npm run dev` serves `public/` at localhost:3000. Review generated CSS if rebuilding;
do not include unrelated generated changes. There is no generic `npm run build`.

Dashboard, from `dashboard/`, in an isolated Python 3.12 environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dashboard.txt pytest pytest-asyncio
PYTHONPATH=src .venv/bin/uvicorn nctqai.main:app --port 5001
PYTHONPATH=src .venv/bin/python -m pytest src/nctqai/tests/test_auth.py
```

Dashboard pytest's default `tests` directory is not the actual suite location;
pass explicit paths. Its requirements file is the web-runtime dependency source,
not a full document/prediction pipeline environment.

Local image builds, from the repository root (not a deployment):

```bash
docker build -t compass-api ./backend
docker build -t compass-fe ./frontend
docker build -t compass-dashboard ./dashboard
```

ADO preserves its own deployment scaffolding, including backend `Dockerfile.api`;
inspect that pipeline before assuming a local build duplicates the live image.

## Style and testing expectations

Follow adjacent code and keep changes focused. Python targets 3.12; backend Ruff
configuration uses a 100-character line length and `F` lint rules. Keep typed
contracts authoritative: do not infer routing again from raw prose below planning,
hardcode district counts, or silently swallow grounding/execution failures.
Preserve frontend SSE, server-side credential handling, accessibility, and exports.
Keep Dashboard roles centralized and backend runtime internals out of its UI.

Bug fixes need focused regression tests. User-visible answer/planner changes also
need approved scenario replay and scorecard evidence: protect grounding and result
correctness, not one planner implementation. Browser changes need actual affected
flow/keyboard/mobile checks; health or unit tests alone are not live acceptance.
For docs-only work, validate local links, referenced paths/commands, consistency,
and `git diff --check`. Report blocked or unrun checks plainly.

## Security and approval

Never commit secrets, `.env` files, credentials, private diagnostics, or personal
conversation data. Keep the API token in PHP/server runtime configuration, never
browser JavaScript. Route model calls through the Pydantic AI Gateway. Inspect
production databases read-only; migrations, secret changes, cloud/network edits,
and deployments need explicit scoped authorization. Do not improvise infrastructure
changes to make a release green.

Work on a feature branch, stage exact scoped files, and open a PR with validation
and evidence limits. **Merging into `main` requires explicit user approval**;
permission to commit, push, or create a PR is not permission to merge or enable
auto-merge. Check the worktree and upstream before committing; preserve others' work.

## Deployment synopsis

The single current operational runbook is
[Hosting, Deployment, and Security](docs/06-hosting-deployment-security.md).
A `main` push changing `backend/**`, `frontend/**`, or `dashboard/**` syncs the
affected application to ADO, preserves its scaffolding, then explicitly queues
its pipeline when there is an effective mirror diff. This includes tests and docs
inside application folders. Root/docs-only and workflow-only edits do not trigger
this path; a workflow-only merge does not exercise deployment.

Treat sync, queue acceptance, Azure build, image/revision/traffic verification,
and live user behavior as separate evidence gates. Coolify staging descriptions
are historical/unverified until an operator confirms the current lane. Never
infer API/Dashboard end-to-end success or retirement of old mirrors from a
frontend rollout.

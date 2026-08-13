# 8. Technical Reference

This page is the implementation map for Compass: the boundaries, interfaces, and
third-party projects a maintainer needs in order to find the right source file. It
answers "where does that live?" and nothing else. It does not explain behavior —
[Start Here](01-start-here.md) routes you to the section that does — and it carries
no secrets, hostnames, or deployment procedures.

## License and open-source acknowledgements

Compass's own repository is released under the [MIT License](../LICENSE) © the
National Council on Teacher Quality. The tables below credit the major open-source
projects Compass uses directly or that provide a visible runtime layer. They are
curated, not a dump of every transitive package. The complete dependency inventory
is in the manifests and lockfiles: `backend/pyproject.toml`, `backend/uv.lock`,
`dashboard/requirements-dashboard.txt`, `frontend/composer.json` and
`composer.lock`, and `frontend/package.json` and `package-lock.json`.

### The MIT License in plain English

The full legal text is [`LICENSE`](../LICENSE) and it governs; this is an
orientation, not a substitute. MIT is one of the shortest and most permissive
open-source licenses in common use. What it means in practice:

**What anyone may do with Compass's code.** Read it, run it, copy it, modify it,
build something else out of it, and distribute or sell that result — including
commercially, and including as part of closed-source software. No permission
request, no fee, no obligation to contribute changes back.

**The one condition.** Keep the copyright notice and the license text with any
substantial copy of the code. That is the whole obligation.

**What NCTQ does not promise.** The software is provided "as is," with no
warranty and no liability. If someone runs a fork of Compass and it produces a
wrong answer, that is on them.

**What the license does *not* cover.** Three things worth being explicit about,
because open-sourcing code is not the same as open-sourcing a product:

- **NCTQ's data and content.** The reviewed policy data, source documents, and
  NCTQ's published positions are not licensed by this file. A fork gets the
  engine, not the answers.
- **The NCTQ name and marks.** MIT grants no trademark rights. A fork may not
  present itself as NCTQ or as Compass.
- **Third-party dependencies.** Every project in the tables below carries its
  own license and its own attribution requirements. Compass's MIT license does
  not override them, and it is not a blanket grant covering the whole
  dependency tree. Before redistributing a built image, review the upstream
  licenses — the manifests and lockfiles named above are the complete list, and
  the browser bundles vendored under `frontend/public/assets/vendor/` retain
  their upstream headers precisely so that attribution trail stays intact.

Each project name links to its official home. Those upstream projects have their
own licenses and attribution requirements; follow them before redistributing a
build. This repository's MIT license does not replace those obligations.

### Runtime, API, and data layer

| Project | What Compass uses it for | Where to look in this repository |
| --- | --- | --- |
| [Python](https://www.python.org/) | Runtime for the backend, dashboard, sync tools, and evaluation scripts | [`backend/pyproject.toml`](../backend/pyproject.toml) |
| [FastAPI](https://fastapi.tiangolo.com/) | HTTP API and OpenAPI surface | [`compass_backend/api/`](../backend/src/compass_backend/api/) |
| [Pydantic](https://docs.pydantic.dev/latest/) | Typed request, response, data, and configuration models | [`compass_backend/contracts/`](../backend/src/compass_backend/contracts/) |
| [Pydantic AI](https://ai.pydantic.dev/) | Typed agent runs, tool calls, and structured model output | [`compass_backend/agents/`](../backend/src/compass_backend/agents/) |
| [Starlette](https://www.starlette.io/) | ASGI foundation beneath FastAPI | Backend web application |
| [Uvicorn](https://www.uvicorn.org/) | ASGI server for the backend and dashboard | [`backend/Dockerfile`](../backend/Dockerfile) |
| [asyncpg](https://github.com/MagicStack/asyncpg) | Async PostgreSQL access on the chat path | [`compass_backend/db/`](../backend/src/compass_backend/db/) |
| [PostgreSQL](https://www.postgresql.org/) | Database engine for the canonical `compass` schema | [Compass schema reference](reference/compass-schema.md) |
| [HTTPX](https://www.python-httpx.org/) | HTTP client and test transport | Backend integrations and tests |
| [sse-starlette](https://github.com/sysid/sse-starlette) | Server-Sent Events responses for streaming chat | [`api/chat_stream.py`](../backend/src/compass_backend/api/chat_stream.py) |
| [ReportLab](https://www.reportlab.com/) | Declared dependency for document/export tooling | `backend/pyproject.toml` |
| [markdown-it-py](https://github.com/executablebooks/markdown-it-py) | Markdown parsing for exported and rendered content | Backend export/content paths |
| [PyYAML](https://pyyaml.org/) | YAML-backed content and configuration parsing | Backend content and sync paths |

### Dashboard, evaluation, and supporting libraries

| Project | What Compass uses it for | Where to look in this repository |
| --- | --- | --- |
| [FastHTML](https://www.fastht.ml/) | Server-rendered internal dashboard UI | [`dashboard/src/nctqai/`](../dashboard/src/nctqai/) |
| [MonsterUI](https://monsterui.answer.ai/) | Dashboard UI components and styling primitives | [`requirements-dashboard.txt`](../dashboard/requirements-dashboard.txt) |
| [Psycopg](https://www.psycopg.org/) | PostgreSQL access for dashboard/reporting paths | `dashboard/requirements-dashboard.txt` |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | Fuzzy matching where a district phrase needs tolerant normalization | [`compass_backend/catalog/`](../backend/src/compass_backend/catalog/) |
| [Typer](https://typer.tiangolo.com/) | Declares the `pa-eval` and `data-fidelity` command-line entry points; the CLI modules themselves are development tooling, outside this snapshot | `backend/pyproject.toml` |
| [pytest](https://pytest.org/) | Automated tests across the backend, sync, and evaluation surfaces | [`compass_backend/tests/`](../backend/src/compass_backend/tests/) |
| [uv](https://docs.astral.sh/uv/) | Python dependency resolution, lockfile installation, and local tooling | [`backend/uv.lock`](../backend/uv.lock) |
| [Pydantic Logfire](https://logfire.pydantic.dev/) | Instrumentation and tracing for the API, database, HTTP, and agent paths | [`compass_backend/observability.py`](../backend/src/compass_backend/observability.py) |

### Public frontend and browser libraries

| Project | What Compass uses it for | Where to look in this repository |
| --- | --- | --- |
| [PHP](https://www.php.net/) | Server-rendered public chat shell and proxy endpoints | [`frontend/public/`](../frontend/public/) |
| [Apache HTTP Server](https://httpd.apache.org/) | Production web server for the PHP frontend and SSE proxy | [`frontend/Dockerfile`](../frontend/Dockerfile) |
| [Tailwind CSS](https://tailwindcss.com/) | Utility-first CSS build for the chat interface | [`frontend/src/input.css`](../frontend/src/input.css) |
| [PostCSS](https://postcss.org/) and [Autoprefixer](https://github.com/postcss/autoprefixer) | CSS processing in the frontend build stage | [`frontend/package.json`](../frontend/package.json) |
| [Chart.js](https://www.chartjs.org/) | Rendering charts returned as Compass artifacts | [`assets/js/utils/chartRenderer.js`](../frontend/public/assets/js/utils/chartRenderer.js) |
| [marked](https://marked.js.org/) | Parsing Markdown in the browser | [`assets/js/utils/markdown.js`](../frontend/public/assets/js/utils/markdown.js) |
| [DOMPurify](https://github.com/cure53/DOMPurify) | Sanitizing browser-rendered Markdown | [`assets/js/utils/markdown.js`](../frontend/public/assets/js/utils/markdown.js) |
| [phpdotenv](https://github.com/vlucas/phpdotenv) | Loading optional local `.env` values for the PHP shell | [`frontend/composer.json`](../frontend/composer.json) |

The Chart.js, marked, and DOMPurify bundles are vendored under
[`frontend/public/assets/vendor/`](../frontend/public/assets/vendor/) and served
same-origin, so a firewall or content filter cannot strand Markdown sanitization
or chart rendering. Their upstream headers and versioned filenames are part of the
attribution trail.

## Application layout and runtime shape

| Application | Runtime boundary | Primary source and build files |
| --- | --- | --- |
| Policy Advisor API | Python 3.12+, FastAPI, Pydantic AI, Uvicorn; default local port `8000` | [`backend/`](../backend/) — `pyproject.toml`, `Dockerfile` |
| Compass Frontend | PHP 8.3 on Apache; Node 20 is used only to build CSS; default local port `3000` for the PHP dev server | [`frontend/`](../frontend/) — `composer.json`, `Dockerfile` |
| NCTQ Dashboard | Python 3.12+, FastHTML, MonsterUI, PostgreSQL; default local port `5001` | [`dashboard/`](../dashboard/) — `pyproject.toml`, `Dockerfile` |
| Data layer | PostgreSQL with `compass` as the canonical schema; data sync and migrations are separate from request handling | [Compass schema reference](reference/compass-schema.md) |

Model providers and the Pydantic AI Gateway are external services, not open-source
dependencies maintained by this repository. The model roles and current instruction
architecture are described in [Product & Answer Flow](02-product-and-answer-flow.md).

## API reference

The backend publishes interactive OpenAPI documentation at `/docs` and `/redoc`.
The table below lists the active route families; request and response fields belong
in the generated OpenAPI schema rather than being copied into this document.

| Method and path | Purpose | Access boundary |
| --- | --- | --- |
| `GET /` | Service/version banner | Public |
| `GET /docs`, `GET /redoc` | Interactive OpenAPI documentation | Public |
| `GET /api/v1/health` | Liveness, version, database status, and auth status | Public |
| `GET /api/v1/ready` | Readiness check with a database probe | Public |
| `POST /api/v1/chat` | Main chat turn; streams SSE unless the client requests JSON | API key when enabled; chat rate limit |
| `POST /api/v1/chat/simple` | Non-streaming chat response | API key when enabled; chat rate limit |
| `GET /api/v1/conversations/{session_id}` | Load a conversation | Session-scoped access |
| `GET /api/v1/conversations/{session_id}/debug` | Load debug backfill data | Admin-only outside development bypass |
| `POST /api/v1/conversations/export` | Create a ZIP conversation export | Session-scoped access |
| `POST /api/v1/feedback` | Submit or update feedback for a message | Session-scoped access |
| `GET /api/v1/feedback?session_id=...` | List feedback for a conversation | Session-scoped access |
| `GET /api/v1/feedback/{session_id}` | Legacy feedback-list path | Session-scoped access |
| `DELETE /api/v1/feedback` | Delete feedback using a request body | Session-scoped access |
| `DELETE /api/v1/feedback/{session_id}/{message_id}` | Legacy feedback-delete path | Session-scoped access |
| `GET /api/v1/scenario-cases` | List B-spine evaluation cases | Admin-only |
| `GET /api/v1/scenario-cases/{case_id}` | Read one B-spine case for review/debugging | Admin-only |
| `GET /api/v1/scenarios`, `GET /api/v1/scenarios/{id}` | Retired compatibility routes; return `410 Gone` | Admin gate, then retired |
| `POST /api/v1/debug/report` | Submit a reviewer report for a debug session/case | Link possession; IP rate-limited |
| `GET /api/v1/debug/report` | Read the latest report for a session/case | Link possession; IP rate-limited |
| `POST /api/v1/debug/report/{report_id}/status` | Update reviewer report status | Link possession; IP rate-limited |

The PHP frontend exposes its own proxy endpoints under
[`frontend/public/api/`](../frontend/public/api/). They are browser-facing adapters,
not a second source of backend business rules. When API-key authentication is
enabled, the backend expects a Bearer key; the key is never documented here or
committed to the repository.

## Configuration reference

Configuration is loaded by the backend's [`Settings`](../backend/src/compass_backend/config.py)
model. Values and secrets belong in the deployment environment, never in this page.
The names below are the main operational groups; the `Settings` model and the
frontend/dashboard source files remain authoritative for the complete list.

| Group | Environment names | Purpose |
| --- | --- | --- |
| Runtime | `HOST`, `PORT`, `DEBUG`, `ENVIRONMENT`, `CORS_ORIGINS` | Bind address, local reload, deployment environment, and browser origins |
| Database | `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_USER`, `PG_PASSWORD`, `PG_SCHEMA` | PostgreSQL connection and canonical schema selection |
| Database pool | `PG_COMMAND_TIMEOUT`, `PG_POOL_MIN_SIZE`, `PG_POOL_MAX_SIZE`, `PG_POOL_MAX_INACTIVE_LIFETIME` | Query timeout and async connection-pool behavior |
| Chat limits | `CHAT_MESSAGE_MAX_CHARS`, `COMPASS_CHAT_RATE_LIMIT_ENABLED`, `COMPASS_CHAT_RATE_LIMIT_PER_MINUTE` | Request size and chat throttling |
| Review protection | `COMPASS_DEBUG_REPORT_RATE_LIMIT_ENABLED`, `COMPASS_DEBUG_REPORT_RATE_LIMIT_PER_MINUTE` | IP-based throttling for unauthenticated debug-report routes |
| Auth and sessions | `API_KEY_AUTH_ENABLED`, `SESSION_STORE_BACKEND`, `TRUSTED_PROXIES` | API-key gate, session persistence, and trusted client-IP handling |
| Catalog | `CATALOG_CANDIDATE_FUSION_ENABLED`, `CATALOG_TOPIC_NARROWING_ENABLED`, `COMPASS_CATALOG_RECALL_SHADOW_ENABLED`, `COMPASS_CATALOG_RESOLVER_RECALL_ENABLED` | Candidate recall and catalog-resolution feature flags |
| Answer layer | `COMPASS_ANSWER_LAYER_MODE`, `COMPASS_ANSWER_LAYER_RESULT_TYPES` | Optional guarded prose rewrite and eligible result types |
| Data defaults | `CURRENT_ACADEMIC_YEAR`, `SELECTION_DEFAULT_LARGEST_LIMIT`, `RANKING_DISPLAY_LIMIT`, `CONVERSATION_MEMORY_RESULT_MAX_BYTES` | Query defaults and bounded response memory |
| Model and observability | `PYDANTIC_AI_GATEWAY_API_KEY`, `LOGFIRE_TOKEN`, `LOGFIRE_READ_TOKEN`, `LOGFIRE_PROJECT` | Model routing credential and tracing configuration |

The frontend has a separate environment boundary. Its important names include
`FASTAPI_CHAT_URL`, `FASTAPI_API_TOKEN`, `FASTAPI_ADMIN_API_TOKEN`,
`FASTAPI_EXPORT_CSV_URL`, `PUBLIC_API_URL`, `DASHBOARD_BASE_URL`,
`COMPASS_CHAT_MESSAGE_MAX_CHARS`, `COMPASS_SSE_PROXY_TIMEOUT`, and
`COMPASS_SSE_DISCONNECT_GRACE_S`. The dashboard has its own database, session,
OTP, mail, Compass API, analytics, and scenario-link settings under
[`dashboard/src/nctqai/config.py`](../dashboard/src/nctqai/config.py).

## Prompts and instruction references

This page does not reproduce system prompts. Three places carry the detail:

- The instruction files themselves, under
  [`backend/src/compass_backend/instructions/`](../backend/src/compass_backend/instructions/),
  whose `README.md` explains the ownership rules and loader behavior.
- The [prompt and model inventory](reference/prompt-and-model-inventory.md) — the
  role-by-role mapping of models, instruction files, guardrails, and fallbacks,
  plus an index of every model-facing asset and how to read its version history.
- The [prompt and instruction history](research/compass-prompt-history/README.md) —
  the design changes and preserved historical snapshots.

The behavioral guardrails themselves — what Compass declines to say and what it
must always disclose — live with the answer flow in
[Product & Answer Flow](02-product-and-answer-flow.md).

Because these files are versioned with the code, their git history is the prompt
version history. A prompt change should therefore be reviewed with the same care as
a code change, while facts, identifiers, coverage, citations, and validation rules
remain owned by typed contracts and ordinary code.

## Pathfinder integration

Compass is embedded as a cross-site iframe on the NCTQ District Policy Pathfinder.
The integration has three parts:

1. **Embed mode.** Add `?embed=true` to the frontend URL. The PHP shell sets the
   `embedMode` flag and `embed-mode` body class, and removes the full-page sidebar
   chrome. Configured in [`frontend/public/index.php`](../frontend/public/index.php)
   and `frontend/src/input.css`.
2. **Frame policy.** The Apache response allows framing by the NCTQ domains used by
   the Pathfinder integration. The policy is configured in
   [`frontend/Dockerfile`](../frontend/Dockerfile); any new host must be reviewed
   there rather than silently adding a wildcard.
3. **Window messaging.** [`embed.js`](../frontend/public/assets/js/embed.js) sends
   `compass:ready` when the iframe initializes and debounced `compass:resize` events
   with the document height. It accepts `compass:prompt` and
   `compass:visitor_id` messages from an allowed NCTQ origin. The parent-issued
   visitor ID is pseudonymous and preferred over the iframe-local fallback; it is
   capped before being attached to a chat request.

The [Pathfinder visual and embed specification](../resources/pathfinder-visual-spec.md)
records the parent/iframe boundary, the current visual baseline, accessibility
expectations, and open design decisions.

The messaging code validates the sender origin for messages received from the
parent. Messages sent to the parent use `postMessage`; the parent must still treat
the payload as untrusted input and validate the message type and payload before
using it.

The visitor ID is an analytics/session continuity aid, not an identity system. It
must not contain personally identifying information. The backend persists it with
the session when supplied; it does not change the grounding or authorization rules
for an answer.

## Keeping this reference current

When a boundary changes, update the source of truth first, then check this page for
links and descriptions that became stale:

- dependency or runtime change → the relevant manifest, lockfile, or Dockerfile;
- route or contract change → the FastAPI router and generated OpenAPI surface;
- data or schema change → the migration and the schema reference;
- prompt behavior change → the instruction file, whose git history is the record;
- embed behavior change → `frontend/public/index.php`,
  `frontend/public/assets/js/embed.js`, or the Apache policy.

This page should explain where a fact lives, not become a second copy of that fact.

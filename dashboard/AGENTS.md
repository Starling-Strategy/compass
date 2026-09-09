# Dashboard engineering guide

This directory is authoritative application source; its Azure DevOps copy is a
deployment projection. Follow [repository guidance](../AGENTS.md) and, for UI
changes, [src/nctqai/AGENTS.md](src/nctqai/AGENTS.md).

## Setup and checks

Run these commands from `dashboard/`, using Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dashboard.txt pytest pytest-asyncio
PYTHONPATH=src .venv/bin/python -m pytest src/nctqai/tests/test_auth.py
```

`requirements-dashboard.txt` is the web-runtime dependency source used by
`Dockerfile`; it is not a complete document/prediction pipeline environment.
`pyproject.toml` contains pytest settings, not an installable project or uv
dependency definition. Its default `tests` path is absent: pass explicit paths
under `src/nctqai/tests/`. Run focused regression tests for the changed behavior;
the auth test above is only a starting check. Live database tests require
separate authorization and `--run-live-db`.

After supplying approved non-production runtime configuration:

```bash
PYTHONPATH=src .venv/bin/uvicorn nctqai.main:app --port 5001
```

Settings live in `src/nctqai/config.py`: generally `NCTQAI_*`, with explicit
aliases for shared settings. Do not point local startup at production:
importing `nctqai.main` attempts database cleanup of expired authentication rows.
Do not assume the `dry_run` setting makes startup read-only. Authentication
bypass is development-only; never use it for staging or production.

Local image build, from `dashboard/`:

```bash
docker build -t compass-dashboard .
```

The container serves port 5001 and checks `/health`. That endpoint reports
process liveness, not database readiness or authenticated workflow acceptance.
UI changes need checks of the affected authenticated flow, roles, HTMX
navigation, keyboard access, and mobile layout.

## Code boundaries and pitfalls

- `src/nctqai/` holds the staff UI, configuration, routes, services, and tests.
  Preserve its specialized instructions and central authorization gates.
- `src/compass_backend/` is a bundled subset used by scorecard code, not the
  full backend. Do not add the sibling backend source to `PYTHONPATH` to hide
  missing bundled dependencies; that would differ from the dashboard image.
- `src/document_pipeline/` and `src/pipelines/` are separate source areas.
  Their presence does not establish that the web image can run those pipelines.
- Keep secrets and `.env` files out of Git. Database migrations, cloud changes,
  and deployments require explicit scoped approval.

[Administration and Dashboard](../docs/05-administration-and-dashboard.md)
explains the staff workflows.
[Hosting, Deployment, and Security](../docs/06-hosting-deployment-security.md)
is the current release authority. Dashboard-folder Markdown and tests can trigger
the dashboard sync/queue lane when merged into `main`. Historical Coolify
instructions and staging hostnames in source do not establish current staging
availability or deployment health.

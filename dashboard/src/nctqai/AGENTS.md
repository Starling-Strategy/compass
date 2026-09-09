# src/nctqai/AGENTS.md

Dashboard service, FastHTML on port 5001. Metric Calculator and Compass
observability have distinct data boundaries; identify the affected surface
before adding routes. Documents, Journal, and administration routes also live here.

## Metric Calculator and Compass

- **Metric Calculator** at `/mc/*`: analyst workflow for reviewing
  AI-suggested policy answers. It writes validated metric data.
- **Compass observability** at `/compass/*`: conversation monitor, scenarios
  UI, verdicts, dimension dashboards, and the Quality scorecard. It reads
  Compass tables.

## Local Dev

Use [dashboard setup and checks](../../AGENTS.md), running commands from
`dashboard/`. Schema overrides are defined in [db.py](db.py);
`PG_SCHEMA=compass` is not a substitute for those settings.

## Import Direction

`nctqai` reads `compass.*` tables. It must not import backend agent internals
for runtime behavior. If it needs backend logic, call the backend over HTTP
or move shared contracts deliberately. Existing scorecard code uses the bundled
`dashboard/src/compass_backend/` subset; this does not authorize full backend
agent-runtime imports.

## Compass Scenario Links

The `/compass/scenarios` launcher should emit durable staging links in the
simple B-spine shape:

```text
https://staging-compass.nctq.ai/?debug=true&case_id=<case_id>
```

Do not add `case_exp`, `case_sig`, or legacy `scenario_id` params to staging
links meant for docs, feedback sheets, issues, PRs, or client update drafts.
Production and other non-staging hosts may still use signed launch params.
This is a source-level URL contract, not confirmation that staging runs. Local
loopback hosts also use unsigned links; other hosts receive signatures only when
a signing secret is configured. Confirm availability through the
[current runbook](../../../docs/06-hosting-deployment-security.md).

## Logfire

Configure and instrument Starlette before middleware registration. Preserve
the ordering in [main.py](main.py).

## Auth & Roles

Human login is email OTP + session cookies; authorization is one central map in
[models/auth.py](models/auth.py) (`Role` enum + `User.can_access`), with additional
route restrictions. See [Administration and Dashboard](../../../docs/05-administration-and-dashboard.md).
Don't add a second role list or a route that bypasses the central gates.

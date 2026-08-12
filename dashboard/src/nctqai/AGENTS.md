# src/nctqai/AGENTS.md

Dashboard service, FastHTML on port 5001. Two unrelated UIs live here;
clarify which one is in scope before adding routes.

## The Two UIs

- **Metric Calculator** at `/mc/*`: analyst workflow for reviewing
  AI-suggested policy answers. It writes validated metric data.
- **Compass observability** at `/compass/*`: conversation monitor, scenarios
  UI, verdicts, dimension dashboards, and the Quality scorecard. It reads
  Compass tables.

## Local Dev

```bash
PG_SCHEMA=compass PYTHONPATH=src uv run uvicorn nctqai.main:app --port 5001
```

## Import Direction

`nctqai` reads `compass.*` tables. It must not import backend agent internals
for runtime behavior. If it needs backend logic, call the backend over HTTP
or move shared contracts deliberately.

## Compass Scenario Links

The `/compass/scenarios` launcher should emit durable staging links in the
simple B-spine shape:

```text
https://staging-compass.nctq.ai/?debug=true&case_id=<case_id>
```

Do not add `case_exp`, `case_sig`, or legacy `scenario_id` params to staging
links meant for docs, feedback sheets, issues, PRs, or client update drafts.
Production and other non-staging hosts may still use signed launch params.

## Logfire

Configure and instrument Starlette before middleware registration. See
[../../docs/logfire-instrumentation-rules.md](../../docs/logfire-instrumentation-rules.md).

## Auth & Roles

Human login is email OTP + session cookies; authorization is one central map in
`models/auth.py` (`Role` enum + `User.can_access`). Full model:
[../../docs/compass_concepts/_permissions-and-user-roles.md](../../docs/compass_concepts/_permissions-and-user-roles.md).
Don't add a second role list or a route that bypasses the central gates.

# 8. Technical Reference

> **Stub — to be drafted.** Durable reference material, kept out of the narrative docs.

Planned contents:

- Open source: what the MIT license means, who can inspect, use, fork, or adapt
  Compass; third-party dependency licenses.
- The technology stack per application, with versions: Python/FastAPI + Pydantic AI
  backend, PHP/Apache frontend, FastHTML dashboard, PostgreSQL, and the Pydantic AI
  Gateway for model routing.
- API endpoint reference for the Policy Advisor API.
- Configuration reference: environment variables and feature flags (names and
  purposes only, never values).
- Appendix — system prompts and instruction sets: the instruction files live in this
  repository under `src/compass_backend/instructions/`; version history is their git
  history.
- Pathfinder website integration: the iframe embed, `?embed=true` mode, the
  postMessage contract (`compass:ready`, `compass:resize`, `compass:visitor_id`),
  and the parent-page visitor-id snippet.

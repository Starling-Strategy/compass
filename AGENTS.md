# Compass engineering guide

## Project and authority

Compass is NCTQ's district-policy research assistant: a public chat interface,
a Policy Advisor API, and an internal staff Dashboard.
This GitHub repository is the authoritative application source.
Azure DevOps application copies are deployment projections, not development homes.
Protected ADO scaffolding and Azure runtime configuration have separate authority.

## Read before changing a component

- [Frontend guide](frontend/AGENTS.md): PHP/Apache interface and API proxy.
- [Backend guide](backend/AGENTS.md): Python/FastAPI answer engine.
- [Dashboard guide](dashboard/AGENTS.md): Python/FastHTML staff tools.
- [README](README.md): project and documentation map.
- [Release runbook](docs/06-hosting-deployment-security.md): deployment and recovery.
- [Provenance](PROVENANCE.md): historical import record, not current live state.

Read the relevant component guide and any deeper `AGENTS.md` before editing,
even when your session starts at the repository root. Component guides own
setup, build/test commands, and implementation conventions; do not duplicate them here.
Backend `instructions/` contains runtime model prompts, not engineering policy.

## Safety and approval

Never commit secrets, `.env` files, credentials, private diagnostics, or personal
conversation data. Keep API credentials server-side and model calls on the
Pydantic AI Gateway. Use approved runtime configuration for local work.
Production database inspection is read-only; never enable operator writes.
Migrations, secret changes, cloud/network changes, deployments, and live/model
tests require explicit user authorization for the particular action and target.
Do not improvise infrastructure changes to make a release green.
Component guidance does not relax these shared safety and approval boundaries.

## Change and review workflow

Work on a feature branch. Check worktree and upstream state before committing;
preserve others' work and stage only exact, scoped files.
Keep changes focused and follow the component's contracts and test guidance.
Bug fixes need regression tests; answer/planner changes need approved scenario
replay and scorecard evidence. Browser changes need affected-flow checks.
For documentation changes, validate links, paths, commands, consistency, and
`git diff --check`. Report blocked or unrun checks and evidence limits plainly.
Open a PR with scope, validation, and deployment impact.
**Merging to `main` requires explicit user approval for that merge.**
Permission to edit, commit, push, or open a PR does not authorize merge or auto-merge.

## Deployment boundary

App-folder changes, including tests and Markdown, trigger affected sync lanes on
a `main` push; root/docs-only and workflow-only changes do not.
Use the release runbook for the exact sync, queue, verification, and recovery
procedure. GitHub success alone is not proof of downstream deployment or live behavior.

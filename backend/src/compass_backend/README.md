# Compass Backend

Active Python/FastAPI answer-engine package. Read the
[backend setup guide](../../AGENTS.md) for commands and dependencies and
[package guardrails](AGENTS.md) before changing runtime behavior.
Use the [release runbook](../../../docs/06-hosting-deployment-security.md)
for deployment; old fresh-workbench Dockerfiles and staging instructions are
not the current release procedure.

Planner context follows the Pydantic AI dependency model. The raw user message
is passed as the agent run prompt, while prior `SessionState`, `QueryContext`,
pending clarification slots, transcript snippets, and safe runtime hints are
passed as typed planner deps. Model-visible context is rendered from those deps
through dynamic Pydantic AI instructions. The chat router owns one lazy cached
planner agent and supplies fresh typed deps on each turn. Provider message
history is persisted as planner evidence for replay/debugging, not as
deterministic execution memory.

Runtime prompt assets live in [instructions/](instructions/).
Follow their [authoring guide](instructions/AGENTS.md); product logic and
live data facts remain in typed Python contracts and execution code.

# Compass Prompt And Prose Guidance

Issue #901 organizes Compass guidance so it is reviewable without weakening the
authority chain. The goal is not a new voice layer yet. The first sprint moves
static model instructions and planner snippets into packaged assets, documents
where other prose belongs, and keeps behavior unchanged.

This document is the ownership reference for prompt/prose surfaces — where each
prompt asset lives and who may change it.

## Mental Model

1. Facts first: typed data, catalog resolution, execution, and validation decide
   what Compass may say.
2. Planning second: Pydantic AI planners choose route and typed query shape.
3. Rendering third: deterministic renderers translate validated artifacts into
   user-facing language.
4. Engagement last: future suggested follow-ups should be structured metadata,
   not hidden prompt prose.

## Inventory

| Surface | Current home | Audience | Can change facts? | Rule |
| --- | --- | --- | --- | --- |
| Static model instructions | `src/compass_backend/instructions/model_instructions/*.md` | Model | No | Review as prompt assets; no drifting data facts, SQL, IDs, or citations. |
| Dynamic Pydantic AI instructions | `@agent.instructions` functions in planner/adjudicator modules | Model | No | Stay in Python because they render typed runtime context. |
| Planner guidance | `src/compass_backend/instructions/planner_guidance/*.md` plus metadata in `planning/instruction_snippets.py` | Model | No | Markdown holds instruction bodies; Python owns deterministic selection. |
| Pydantic field descriptions | `contracts/*`, `artifacts/*`, `db/rows.py`, quality models | Model/schema readers | No | Stay beside fields; describe contract semantics, not persona or answer style. |
| Answer style guides | `src/compass_backend/instructions/answer_style_guides/*.md` | User via answer stylist | No | Rewrites sealed answer briefs; facts, coverage, citations, and immutable blocks remain typed. |
| Renderer copy | `rendering/*` and `artifacts/coverage.py` | User | No | Stays with typed rendering logic unless moved with snapshots and validators. |
| NCTQ policy content | `content/nctq-policy/topics/*` | User via governed route | Source content only | Treat as domain source material, not prompt text. |
| Quality rubrics and judge instructions | `quality/*`, `prompts/model_instructions/judge.md`, `compass.criteria` | Evaluators | No | Rubrics evaluate responses after rendering; they do not alter answers. |
| Follow-up prompts/chips | Future `manifest.suggested_followups` | User | No | Should be typed response metadata generated after validation. |
| Model routing | `agents/model_settings.py` | Runtime | No | Keep separate from prompt text so cost/speed choices do not rewrite guidance. |

## Pydantic AI Usage

Static instructions are loaded from markdown and passed to `Agent(...,
instructions=...)` or the relevant Pydantic AI toolset. Runtime context stays in
dynamic instruction functions because it depends on typed deps, selected planner
guidance, recognition reports, and authority warnings.

Agent specs are intentionally deferred. The planner binds runtime
`Literal[*topic_ids]` output types, output validators, diagnostic hooks, and
dynamic instructions; moving that to YAML would hide important runtime
composition. Smaller stable agents can be reconsidered later.

## Change Process

- Move prompt text only when tests prove loaded text matches the prior prompt or
  the PR explicitly calls out the behavior change.
- Add or change planner snippets with selector tests that prove when they do
  and do not appear.
- Keep renderer voice changes in renderer-focused PRs with rendered-output
  assertions.
- Keep model changes in model-settings PRs; do not bury model routing in
  prompt markdown.

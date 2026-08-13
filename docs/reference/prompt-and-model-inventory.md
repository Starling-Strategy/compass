# Prompt and model inventory

The current map of Compass's model-backed roles: which model each role uses,
where its instructions live, what keeps it inside its authority, and what happens
when the model call can't be used.

Model names are code defaults as of 2026-08-12 and can be overridden per role
with the matching `COMPASS_AGENT_*_MODEL` environment variable. The authoritative
list is
[`agents/model_settings.py`](../../backend/src/compass_backend/agents/model_settings.py).
This page indexes the instruction and implementation files; it does not reproduce
their prompt text.

## Roles at a glance

| Role | Default model | Instructions |
| --- | --- | --- |
| Planner | `gateway/anthropic:claude-sonnet-4-6` | [`model_instructions/planner.md`](../../backend/src/compass_backend/instructions/model_instructions/planner.md) plus selected `planner_guidance/` snippets |
| Answer stylist | `gateway/anthropic:claude-opus-4-6` | [`answer_style_guides/default.md`](../../backend/src/compass_backend/instructions/answer_style_guides/default.md) |
| Clarify stylist | `gateway/anthropic:claude-opus-4-6` | [`model_instructions/clarify_stylist.md`](../../backend/src/compass_backend/instructions/model_instructions/clarify_stylist.md) |
| Catalog adjudicator | `gateway/anthropic:claude-haiku-4-5` | [`model_instructions/catalog_adjudicator.md`](../../backend/src/compass_backend/instructions/model_instructions/catalog_adjudicator.md) |
| Criterion classifier | `gateway/anthropic:claude-haiku-4-5` | [`model_instructions/criterion_classifier.md`](../../backend/src/compass_backend/instructions/model_instructions/criterion_classifier.md) |
| Judge | `gateway/anthropic:claude-haiku-4-5` | [`model_instructions/judge.md`](../../backend/src/compass_backend/instructions/model_instructions/judge.md) plus each criterion's live rubric |

Throughout this page, **fallback** means the recovery path when a model result
fails, is invalid, or is unavailable. It never means Compass silently substitutes
a different model.

## Planner

**Guardrails.** Returns a typed `PlannerTurn`. Output retries are disabled, so an
invalid plan is never silently re-rolled. Catalog lookups it performs are advisory
candidates only — execution re-resolves them. Validators and plan finalization
live in `planning/planner.py` and the code around it. Question-specific
`planner_guidance/` snippets are chosen deterministically by
`planning/instruction_snippets.py`, not by the model.

**Fallback.** Unsupported thinking settings are retried without thinking. A
structured-output failure becomes a typed rescue clarification through
`orchestration/structured_output_failure.py`, with a small set of known governed
repairs. It does not invent an answer.

## Answer stylist

**Guardrails.** Runs only after deterministic execution, validation, and rendering
have produced a sealed `AnswerBrief`. The validator in
`answer_layer/validation.py` protects immutable tables and source lists, numeric
tokens, caveats, source markers, and NCTQ context. Mode and eligible result types
are configuration-controlled.

**Fallback.** On timeout, exception, or validation rejection,
`answer_layer/service.py` returns the deterministic renderer body. In `shadow`
mode the stylist output is reported but never replaces that body.

## Clarify stylist

**Guardrails.** Receives a typed `ClarifyBrief` holding the user's metric phrase
and grounded candidate labels, and produces one clarifying question. It does not
choose data, identifiers, or facts.

**Fallback.** On any failure or invalid draft, `answer_layer/clarify.py` uses the
deterministic clarification text from `_clarify_helpers.py`, preserving the
grounded candidates and the clarification route.

## Catalog adjudicator

**Guardrails.** Receives only a bounded candidate set. Output validation rejects
any identifier outside that set and enforces the action/selection contract. One
output retry is allowed, because the choice is finite.

**Fallback.** If adjudication fails, `planning/catalog_pipeline.py` keeps the
original unadjudicated resolution and records a skip/error marker.
High-confidence and single-candidate resolutions skip the model entirely.

## Criterion classifier

**Guardrails.** The output schema is built from the active `compass.criteria`
codes, which makes a fabricated code impossible at the schema boundary. Model
selection is the last of four safeguards and is additive only: the mandatory,
scenario, and deterministic prefilter safeguards cannot be removed. The policy
lives in `quality/classifier.py`.

**Fallback.** If the model call fails, Compass uses the union from the first three
safeguards. Evaluation continues with reduced recall rather than failing the turn.

## Judge

**Guardrails.** Judges a rendered response against one rubric; it never generates
or rewrites a user-facing answer. The output is a typed three-state judgment
(`pass`, `fail`, or `not_applicable`), and the quality layer records the model,
rubric, and evidence alongside the verdict.

**Fallback.** Missing gateway credentials or a judge exception produces an
`outcome="error"` verdict with evidence. `not_applicable` is recorded as an
excluded result. There is no silent alternate-model judgment.

## Supporting instruction surfaces

These are part of the prompt system but are not separate model roles. All live
under
[`backend/src/compass_backend/instructions/`](../../backend/src/compass_backend/instructions/):

- `model_instructions/` — stable, always-on instructions for model-facing agents
  and toolsets.
- `planner_guidance/` — small, on-demand planner snippets. Python owns their
  selection and injects only those matching the current typed context.
- `model_instructions/catalog_toolset.md` — the planner's advisory catalog-recall
  tool. It returns candidate cards only; execution still resolves and verifies
  the final catalog entities.
- `answer_style_guides/` — user-facing style guidance for rewriting sealed answer
  briefs. It owns no data truth, coverage state, citation, or renderer decision.
- `README.md` — how the assets are loaded and which responsibilities stay in
  Python.

## How to read and update this inventory

The source of truth is split by responsibility: model defaults and environment
overrides in `agents/model_settings.py`; static prompt wording in the markdown
instruction assets; dynamic context, typed output contracts, validators, retries,
timeouts, fallbacks, and telemetry in the Python implementation files named above.
The rationale for the split across models is in [§2 Product & Answer
Flow](../02-product-and-answer-flow.md#how-compass-uses-different-ai-models).

When a role, default model, instruction asset, or fallback changes, update the
code or prompt file first, then refresh this page. Keep it an index, not a second
copy of the prompts.

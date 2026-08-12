# Prompt and model inventory

This is the current map of Compass's model-backed roles. It answers, in one
place, which model each role uses, where its instructions live, what keeps it
within its authority, and what happens when the model call cannot be used.

The model names below are code defaults as of 2026-08-12. They can be replaced
at runtime with the corresponding `COMPASS_AGENT_*_MODEL` environment variable;
the authoritative settings are in
[`agents/model_settings.py`](../../backend/src/compass_backend/agents/model_settings.py).
The links in this page point to the live instruction and implementation files;
this page intentionally does not reproduce their prompt text.

## Runtime roles

“Fallback” means the recovery path for a failed, invalid, unavailable, or
rejected model result. It does not mean that Compass silently substitutes a
different model.

| Role | Default model | Instruction file | Guardrails | Fallback |
| --- | --- | --- | --- | --- |
| Planner | `gateway/anthropic:claude-sonnet-4-6` | [`model_instructions/planner.md`](../../backend/src/compass_backend/instructions/model_instructions/planner.md), with question-specific [`planner_guidance/`](../../backend/src/compass_backend/instructions/planner_guidance/) selected by [`instruction_snippets.py`](../../backend/src/compass_backend/planning/instruction_snippets.py) | Returns a typed [`PlannerTurn`](../../backend/src/compass_backend/contracts/planning.py); output retries are disabled so an invalid plan is not silently re-rolled; catalog results are advisory candidates and execution re-resolves them; validators and finalization keep grounding and query shape in [`planning/planner.py`](../../backend/src/compass_backend/planning/planner.py) and code around it. | Unsupported thinking settings are retried without thinking. A structured-output failure becomes a typed rescue clarification through [`structured_output_failure.py`](../../backend/src/compass_backend/orchestration/structured_output_failure.py), with a small set of known governed repairs; it does not invent an answer. |
| Catalog adjudicator | `gateway/anthropic:claude-haiku-4-5` | [`model_instructions/catalog_adjudicator.md`](../../backend/src/compass_backend/instructions/model_instructions/catalog_adjudicator.md) | Receives only a bounded candidate set through [`CatalogAdjudicationDeps`](../../backend/src/compass_backend/catalog/adjudication.py); output validation rejects IDs outside that set and enforces the action/selection contract; one output retry is allowed because the choice is finite. | If adjudication fails, [`catalog_pipeline.py`](../../backend/src/compass_backend/planning/catalog_pipeline.py) keeps the original unadjudicated catalog resolution and records a skip/error marker. High-confidence or single-candidate resolutions can skip the model entirely. |
| Criterion classifier | `gateway/anthropic:claude-haiku-4-5` | [`model_instructions/criterion_classifier.md`](../../backend/src/compass_backend/instructions/model_instructions/criterion_classifier.md) | The output schema is built from the active `compass.criteria` codes, making fabricated codes impossible at the schema boundary. AI selection is Safeguard 4 and additive only; mandatory, scenario, and deterministic prefilter safeguards cannot be removed. The selection policy is in [`quality/classifier.py`](../../backend/src/compass_backend/quality/classifier.py). | If the AI call fails, Compass returns the union from Safeguards 1–3. Evaluation continues conservatively with reduced recall rather than failing the turn. |
| Judge | `gateway/anthropic:claude-haiku-4-5` | [`model_instructions/judge.md`](../../backend/src/compass_backend/instructions/model_instructions/judge.md), plus the live rubric in each [`compass.criteria`](../../backend/src/compass_backend/quality/criteria.py) record | Judges a rendered response against one rubric; it does not generate or rewrite user answers. The output is a typed three-state judgment (`pass`, `fail`, or `not_applicable`), and the quality layer records the model, rubric, and evidence with the verdict. | Missing gateway credentials or a judge exception produces an `outcome="error"` verdict with evidence. `not_applicable` is recorded as an excluded evaluation result; there is no silent alternate-model judgment. |
| Answer stylist | `gateway/anthropic:claude-opus-4-6` | [`answer_style_guides/default.md`](../../backend/src/compass_backend/instructions/answer_style_guides/default.md), loaded by [`answer_layer/agent.py`](../../backend/src/compass_backend/answer_layer/agent.py) | Runs only after deterministic execution, validation, and rendering have produced a sealed [`AnswerBrief`](../../backend/src/compass_backend/answer_layer/briefs.py). The validator in [`answer_layer/validation.py`](../../backend/src/compass_backend/answer_layer/validation.py) protects immutable tables/source lists, numeric tokens, caveats, source markers, and NCTQ context; mode and eligible result types are configuration-controlled. | On timeout, exception, or validation rejection, [`answer_layer/service.py`](../../backend/src/compass_backend/answer_layer/service.py) returns the deterministic renderer body. In `shadow` mode the stylist output is reported but never replaces that body. |
| Clarify stylist | `gateway/anthropic:claude-opus-4-6` | [`model_instructions/clarify_stylist.md`](../../backend/src/compass_backend/instructions/model_instructions/clarify_stylist.md), loaded by [`answer_layer/clarify_agent.py`](../../backend/src/compass_backend/answer_layer/clarify_agent.py) | Receives a typed [`ClarifyBrief`](../../backend/src/compass_backend/contracts/answer_layer.py) containing the user's metric phrase and grounded candidate labels. It produces one `ClarifyDraft` question; it does not choose data, IDs, or facts. | On any stylist failure or invalid draft, [`answer_layer/clarify.py`](../../backend/src/compass_backend/answer_layer/clarify.py) uses the deterministic `_metric_clarification` f-string from [`_clarify_helpers.py`](../../backend/src/compass_backend/_clarify_helpers.py), preserving the grounded candidates and the clarification route. |

## Supporting instruction surfaces

These are important parts of the prompt system but are not separate model
roles:

- [`model_instructions/`](../../backend/src/compass_backend/instructions/model_instructions/)
  contains stable, always-on instructions for model-facing agents and toolsets.
- [`planner_guidance/`](../../backend/src/compass_backend/instructions/planner_guidance/)
  contains small, on-demand planner snippets. Python owns their deterministic
  selection and injects only the snippets that match the current typed/runtime
  context.
- [`catalog_toolset.md`](../../backend/src/compass_backend/instructions/model_instructions/catalog_toolset.md)
  describes the planner's advisory catalog-recall tool. The tool returns
  candidate cards only; the execution path still resolves and verifies the
  final catalog entities.
- [`answer_style_guides/`](../../backend/src/compass_backend/instructions/answer_style_guides/)
  contains user-facing style guidance for rewriting sealed answer briefs. It
  does not own data truth, coverage state, citations, or renderer decisions.
- [`instructions/README.md`](../../backend/src/compass_backend/instructions/README.md)
  explains how the assets are loaded and which runtime responsibilities stay
  in Python.

## How to read and update this inventory

The source of truth is split deliberately by responsibility:

- Model defaults and environment overrides live in
  [`agents/model_settings.py`](../../backend/src/compass_backend/agents/model_settings.py).
- Static prompt wording lives in the linked Markdown instruction assets.
- Dynamic context, typed output contracts, validators, retries, timeouts,
  fallbacks, and telemetry live in the linked Python implementation files.
- The model-selection rationale and trade-offs are summarized in
  [Product & Answer Flow](../02-product-and-answer-flow.md#how-compass-uses-different-ai-models).
- The broader ownership rules for prompts, facts, rendering, and evaluation
  are in [`_prompt-and-prose-guidance.md`](../../backend/src/compass_backend/instructions/_prompt-and-prose-guidance.md)
  and the backend [`AGENTS.md`](../../backend/src/compass_backend/AGENTS.md).

When a role, default model, instruction asset, or fallback changes, update the
authoritative code or prompt file first, then refresh this table. Keep this
page as an index and orientation layer rather than a second copy of the
prompts.

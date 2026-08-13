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

## Complete instruction-asset inventory

This is the exhaustive list of every instruction asset that shapes a Compass
model call — the answer to "show us all the system prompts." Each row links to
the current, final version of that file. Nothing model-facing is omitted: if a
model reads it, it is in this table.

The files are not reproduced inline on purpose. A pasted copy of a prompt is
stale the moment the file changes, and a reader comparing a pasted excerpt
against a running system has no way to know which one is current. The links are
the deliverable; the section below explains how to read their version history.

### Always-on model instructions

Loaded on every call for that role.

| File | Role it instructs | What it governs |
| --- | --- | --- |
| [`model_instructions/planner.md`](../../backend/src/compass_backend/instructions/model_instructions/planner.md) | Planner | The planner's contract: route selection, typed-plan field rules, filter and limit encoding, metric and lane handling, follow-up behavior, and worked examples. The largest asset in the set. |
| [`model_instructions/catalog_toolset.md`](../../backend/src/compass_backend/instructions/model_instructions/catalog_toolset.md) | Planner (tool description) | How the planner's advisory catalog-recall tool behaves and why its results are candidates, not authority. |
| [`model_instructions/catalog_adjudicator.md`](../../backend/src/compass_backend/instructions/model_instructions/catalog_adjudicator.md) | Catalog adjudicator | Choosing among a supplied candidate set when a phrase is genuinely ambiguous. |
| [`model_instructions/clarify_stylist.md`](../../backend/src/compass_backend/instructions/model_instructions/clarify_stylist.md) | Clarify stylist | Phrasing one grounded clarifying question from a typed brief. |
| [`model_instructions/criterion_classifier.md`](../../backend/src/compass_backend/instructions/model_instructions/criterion_classifier.md) | Criterion classifier | Selecting which evaluation criteria apply to a delivered response. |
| [`model_instructions/judge.md`](../../backend/src/compass_backend/instructions/model_instructions/judge.md) | Quality judges | Grading one response against one rubric and returning a three-state verdict. |
| [`answer_style_guides/default.md`](../../backend/src/compass_backend/instructions/answer_style_guides/default.md) | Answer stylist | Voice, answer shape, coverage strings, NCTQ-context policy, jargon substitutions, and the hard rules. The source for [§2's guardrails](../02-product-and-answer-flow.md#guardrails-what-compass-must-not-say-and-what-it-must-always-say). |

### On-demand planner guidance

Small topic snippets in [`planner_guidance/`](../../backend/src/compass_backend/instructions/planner_guidance/),
selected deterministically by [`instruction_snippets.py`](../../backend/src/compass_backend/planning/instruction_snippets.py)
on word-boundary trigger phrases, blocked phrases, prior-route requirements, and
priority — **capped at three per question**, so no single turn sees the whole
set. Each is injected with an explicit warning that it carries no execution,
catalog, or citation authority, and the selection is persisted with the turn.

These are the accumulated lessons about NCTQ's specific content: each one exists
because a real question shape was answered worse than the data supported.

| Snippet | When it applies |
| --- | --- |
| [`anchor-value-filter.md`](../../backend/src/compass_backend/instructions/planner_guidance/anchor-value-filter.md) | "Same value as [anchor district]" — an equality filter, not a peer request |
| [`compensation-salary-exemplar.md`](../../backend/src/compass_backend/instructions/planner_guidance/compensation-salary-exemplar.md) | Bare subjective superlatives about teacher pay ("best compensation") |
| [`coverage-state-language.md`](../../backend/src/compass_backend/instructions/planner_guidance/coverage-state-language.md) | The user asks why data is missing, older, unranked, or partly covered |
| [`data-inventory.md`](../../backend/src/compass_backend/instructions/planner_guidance/data-inventory.md) | "What data do you have about X" — a directory request, not a query |
| [`differentiated-pay-inventory.md`](../../backend/src/compass_backend/instructions/planner_guidance/differentiated-pay-inventory.md) | "Differentiated pay" asked without naming which type |
| [`district-specific-absence.md`](../../backend/src/compass_backend/instructions/planner_guidance/district-specific-absence.md) | A metric is inapplicable because of state law or bargaining status — phrase it per district, not per state |
| [`follow-up-reference.md`](../../backend/src/compass_backend/instructions/planner_guidance/follow-up-reference.md) | "Those districts", "name them", "the ones from before" |
| [`health-benefit-exemplar.md`](../../backend/src/compass_backend/instructions/planner_guidance/health-benefit-exemplar.md) | Subjective superlatives about health benefits |
| [`parental-leave-beyond-birthing.md`](../../backend/src/compass_backend/instructions/planner_guidance/parental-leave-beyond-birthing.md) | Parental leave asked about non-birthing, adoptive, or foster lanes |
| [`peer-policy-comparison.md`](../../backend/src/compass_backend/instructions/planner_guidance/peer-policy-comparison.md) | An anchor district plus peers plus a governed policy topic |
| [`peer-salary-comparison.md`](../../backend/src/compass_backend/instructions/planner_guidance/peer-salary-comparison.md) | Maximum teacher salary across peers of one anchor district |
| [`policy-guidance-advisory-followup.md`](../../backend/src/compass_backend/instructions/planner_guidance/policy-guidance-advisory-followup.md) | After a guidance turn: "should we prioritize pay or benefits?" |
| [`policy-guidance-followups.md`](../../backend/src/compass_backend/instructions/planner_guidance/policy-guidance-followups.md) | After a guidance turn: details, sources, contracts, or narrowing by region |
| [`profile-sort-metric-display.md`](../../backend/src/compass_backend/instructions/planner_guidance/profile-sort-metric-display.md) | Rank by a profile field (enrollment, FRPL) but display a policy metric |
| [`profile-sort-salary-display.md`](../../backend/src/compass_backend/instructions/planner_guidance/profile-sort-salary-display.md) | The salary-specific case of the same pattern |
| [`ranking-and-sorting.md`](../../backend/src/compass_backend/instructions/planner_guidance/ranking-and-sorting.md) | Any ranking, sorting, top/bottom, or ordered request |
| [`salary-schedule-lookup.md`](../../backend/src/compass_backend/instructions/planner_guidance/salary-schedule-lookup.md) | "What's [district]'s salary schedule" — an overview, not one metric |
| [`sick-leave-ranking.md`](../../backend/src/compass_backend/instructions/planner_guidance/sick-leave-ranking.md) | Ranking paid sick/leave days scoped to Texas, where the sick/personal distinction matters |
| [`similarity-discovery.md`](../../backend/src/compass_backend/instructions/planner_guidance/similarity-discovery.md) | "Who are [district]'s peers?" |
| [`teacher-compensation-salary.md`](../../backend/src/compass_backend/instructions/planner_guidance/teacher-compensation-salary.md) | Salary requests involving degree lanes, thresholds, or multi-lane rankings |
| [`teacher-evaluation-observations.md`](../../backend/src/compass_backend/instructions/planner_guidance/teacher-evaluation-observations.md) | Observation counts asked without specifying the observation lane |

### Authoring standards (read by maintainers, not by models)

| File | Purpose |
| --- | --- |
| [`README.md`](../../backend/src/compass_backend/instructions/README.md) | Loader behavior and which responsibilities stay in Python |
| [`AGENTS.md`](../../backend/src/compass_backend/instructions/AGENTS.md) | Working rules for changing an instruction asset |
| [`HOUSE_STYLE.md`](../../backend/src/compass_backend/instructions/HOUSE_STYLE.md) | House style the instruction files themselves follow, enforced by lint tests |
| [`_prompt-and-prose-guidance.md`](../../backend/src/compass_backend/instructions/_prompt-and-prose-guidance.md) | The ownership rule: which truths belong in code rather than prompt prose |
| [`answer_style_guides/README.md`](../../backend/src/compass_backend/instructions/answer_style_guides/README.md) | How style guides are selected and scoped |

### Non-prompt content the model may read

Distinct from instructions: NCTQ's own policy positions, rationales, and
exemplars are reviewed *content*, parsed and rendered deterministically rather
than interpreted as instructions. They live in
[`backend/content/nctq-policy/`](../../backend/content/nctq-policy/README.md),
one file per topic. See the limitation in
[§9](../09-known-issues-and-limitations.md#nctq-policy-guidance-is-a-managed-markdown-stopgap).

## Version history

There is no separate prompt changelog, and that is the design: **the git history
of these files *is* the prompt version history.** Every change to an
instruction asset went through the same review as a code change, with an author,
a date, a message, and a diff.

To read the history of any file above:

```bash
# Every change to one instruction file, newest first.
git log --follow -p -- backend/src/compass_backend/instructions/model_instructions/planner.md

# What changed across all instruction assets in a date range.
git log --since=2026-05-01 --stat -- backend/src/compass_backend/instructions/

# The exact version of a prompt that was live at a given commit.
git show <commit>:backend/src/compass_backend/instructions/answer_style_guides/default.md
```

Because [PROVENANCE.md](../../PROVENANCE.md) pins the commit each production
image was built from, that last command is also how to recover the precise
prompt text that produced a specific historical answer.

For the narrative history — the earlier single-agent and multi-agent designs,
why each was replaced, and preserved extracts of the retired prompts — see
[Prompt and instruction history](../research/compass-prompt-history/README.md)
and its dated [snapshots](../research/compass-prompt-history/).

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

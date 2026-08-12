# src/compass_backend/AGENTS.md

Active Compass backend. Run source-first with `PYTHONPATH=src`. Root
`AGENTS.md` holds cross-cutting workflow; this file is backend-specific.

## Backend Guardrails

1. Coverage derives from `compass.district_profiles`, not prompt literals,
   hardcoded counts, or historical constants.
2. Don't hardcode DB IDs, district counts, or universe rubrics in prompts.
   Let the Planner and catalog/execution tools resolve current data. Static
   prompt assets live under `instructions/`; see
   [_prompt-and-prose-guidance.md](instructions/_prompt-and-prose-guidance.md).
3. Deleted Compass packages are git-history reference only. Port the smallest
   durable behavior into `src/compass_backend/`; don't reintroduce imports.
4. No prose dispatch below the planning boundary — code under `execution/`, `catalog/`,
   `orchestration/`, or post-planner normalizers must not re-read raw user prose
   to route, rewrite, or substitute for a typed field. Enforced by
   `tests/test_no_prose_dispatch.py`; existing exceptions live in
   `tests/fixtures/prose_dispatch_baseline.json` with a role justification, and
   new work reduces that baseline. Allowed shapes (validators, candidate
   broadeners, typed referent resolvers) vs. the forbidden second-interpreter
   pattern, plus the governed typed repair loop:
   [_structured-plans-not-prose.md](../../docs/compass_concepts/_structured-plans-not-prose.md).
5. Bug fixes need focused tests under `src/compass_backend/tests/` plus a
   B-spine regression case when behavior is user-visible.
6. **Keep functions under ~200 lines.** Split orchestration, planning,
   rendering, and quality changes into focused helpers before adding logic to
   existing hot-path modules.
7. **These guards enforce the *ends*, not a planner shape.** Guardrails 1, 2,
   and 4 are correctness-determinism — they hold regardless of how planning
   evolves. Anchor any new grounding or route-integrity check at the
   execution/result boundary (the `ResultSet`, answer text, sources, freshness),
   not to the current planner topology, so it survives the #1248 planning
   redesign. Frame:
   [00-architecture → Invariants: ends vs. means](../../docs/compass_concepts/00-architecture.md#invariants-ends-vs-means).
8. **Read the *finalized* plan, never the legacy slot.** `finalize_plan`
   canonicalizes the planner draft before execution: it folds a top-level
   `plan.sort` into a `presentation`-phase `SortStepSpec` and clears
   `plan.sort = None`, and rebalances `metrics`↔`profile_fields`. So under
   `execution/`, `quality/`, `rendering/`, `orchestration/` the sort lives in
   `plan.sort_steps`, not `plan.sort`. Resolve sort through the canonical
   accessors in [`execution/selection.py`](execution/selection.py) —
   `direction`, `primary_sort_step`, `sort_field`, `sort_kind` — and an executor
   and its consistency-validator must read the **same** accessor (else they
   share a stale default and the validator goes blind). Reading the emptied
   `plan.sort` (or the pre-rebalance metric slot) silently reverts to a wrong
   default — the S41/S107 "ranked lowest first" → "highest to lowest" miss and
   the lookup-sort cluster (#1315). Pin new surfaces with a round-trip test in
   [`tests/test_finalizer_boundary_invariants.py`](tests/test_finalizer_boundary_invariants.py).

8b. **Numeric value coercion — route metric/profile/filter values through
   `parse_compass_numeric_value`** (the single authority in
   [`execution/_text_utils.py`](execution/_text_utils.py)); never bare
   `float()`/`int()` on a value field. Propagate `None` for unparseable input,
   never raise — a bare `float("45,602")` is a `ValueError` that 500s the turn
   (#1772 / #1773 / #1774). A metric row's value is parsed **once, at the row
   boundary**: `MetricAnswerRow` (in [`db/rows.py`](db/rows.py)) runs the parser
   in a `model_validator` and stores the result in `numeric_value`; numeric
   consumers (filter, count, ranking, lookup-sort, trend) read that typed field,
   they never re-parse `value`. The only direct callers left parse a *threshold*
   or *predicate input* (the filter/anchor comparison value, the count threshold,
   the strict-positive lookup predicate), not a row value. Enforced by
   [`tests/test_no_bare_value_coercion.py`](tests/test_no_bare_value_coercion.py).

## Error-handling posture (validate-reject / derive-recover / swallow)

Three failure responses, chosen by *where in the pipeline* the failure happens.
Pick the wrong one and you either turn an answerable turn into a clarification
or silently lose a real result.

- **VALIDATE-REJECT — at the generative planner boundary.** The planner agent
  runs `retries={"output":0}` ([`planning/planner.py`](planning/planner.py)),
  so a `ModelRetry` raised by an `@agent.output_validator`, *or* a
  `ValidationError` from a `QueryPlan` / `PlannerTurn` field or
  `model_validator`, is NOT retried — it surfaces as `UnexpectedModelBehavior`,
  which orchestration converts to a rescue clarification
  (`is_rescue_fallback=True`; see
  [`orchestration/structured_output_failure.py`](orchestration/structured_output_failure.py)
  and [`orchestration/chat.py`](orchestration/chat.py)). **Consequence:** any
  *new* rejecting validator on planner output is user-visible — it can turn an
  answered turn into a clarification — so it is **eval-gated**: prove it on the
  scorecard before merge (guardrail 5 + the Milestone Execution ends-vs-means
  rule in the root `AGENTS.md`). The discriminative
  `CatalogAdjudicator` inverts this on purpose with `retries={"output":1}`
  ([`catalog/adjudication.py`](catalog/adjudication.py)): its candidate set is
  finite, so a single retry is cheap and almost always correct.

- **DERIVE-RECOVER — in catalog/execution, from TYPED fields only.** When a
  plan is slightly imperfect but valid, repair it from the typed fields it
  already carries — never by re-reading the user's prose (guardrail 4, no prose
  dispatch). Examples: the `filter_kind` field-derivation fallback
  ([`execution/filters.py`](execution/filters.py)), `finalize_plan`
  canonicalization ([`planning/finalizer.py`](planning/finalizer.py)),
  `split_state_suffix` in
  [`execution/referent_resolution.py`](execution/referent_resolution.py) (a
  pure typed-string splitter, not a prose reader), and the `degree_lane`
  classifier ([`execution/scoping.py`](execution/scoping.py)). Each such
  fallback is **scaffolding to retire** once the planner reliably emits the
  typed field (proven on the scorecard) — it compensates for a planner gap, it
  is not a permanent layer.

- **SWALLOW — only in non-blocking courtesy layers, always with a marker.**
  Reserved for fire-and-forget side effects that must never fail the
  already-returned turn: post-turn L2 verdict writes
  ([`quality/verdict_writer.py`](quality/verdict_writer.py)) and shape-guard
  enrichment ([`orchestration/chat.py`](orchestration/chat.py)). Even then,
  **log AND leave a marker** so the loss stays observable — an
  `outcome="error"` verdict row, `is_rescue_fallback`, or a
  `*_skipped` / `*_skip_reason` span attribute. **Never swallow an
  execution, grounding, or result-validation failure** — those are the *ends*
  the guardrails protect (root `AGENTS.md` ends-vs-means), and a swallowed
  grounding failure is an invented answer.

One-line decision tree: **planner OUTPUT failure?** → validate-reject (and know
it is eval-gated). **Post-planner repair from typed fields?** → derive-recover.
**Non-blocking side effect (telemetry / courtesy)?** → swallow-with-marker. A
grounding or execution failure is never silently swallowed.

## Settings And Secrets

Settings read `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_USER`, `PG_PASSWORD`,
and `PG_SCHEMA` directly. Secret fields (`pg_password`,
`pydantic_ai_gateway_api_key`, `logfire_token`, `staging_slack_bot_token`)
are `SecretStr` — call `.get_secret_value()` before passing them to asyncpg,
the gateway client, or subprocesses.

## Models And Gateway

All Claude and Gemini fallback calls route through the Pydantic AI Gateway
using `PYDANTIC_AI_GATEWAY_API_KEY`. Agents use model strings from
`src/compass_backend/agents/model_settings.py`. Don't introduce direct
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` bypasses.

How current model choices were made — selection framework, A/B findings,
known Sonnet weaknesses, and how to re-run experiments:
[how-we-pick-models.md](../../docs/how-we-pick-models.md).
The harness itself lives at [scripts/model_ab/](../../scripts/model_ab/).

## Where Guidance Lives

This applies the root model — *one authority per fact; to change a fact you edit
one file.* The root [`AGENTS.md`](../../AGENTS.md) §"Skills, Instructions, and
Docs" states that model once; it is not recounted or restated here. The backend
specifics for each surface, **authority first** (code owns the fact; everything
below references it):

- **Facts, labels, rules, thresholds → code.** One registry/validator per domain
  (`artifacts/coverage.py`, `reference/states.py`, …) — enforceable, and the
  single authority for any value an answer can contain. The renderer, seal,
  validators, and count buckets **import** it; they don't restate it.
- **Runtime agent behavior → `instructions/`** (the Pydantic AI `instructions=`
  payload — how Compass's *own* agents phrase, route, reason). Two tiers:
  **base** = always-on static instructions, one per agent (`model_instructions/`,
  `answer_style_guides/`); **supplemental** = on-demand snippets selected per
  question and injected as dynamic `@agent.instructions` (`planner_guidance/` +
  [`planning/instruction_snippets.py`](planning/instruction_snippets.py) — Compass's
  "skills" tier). All agents load these through the one cached loader in
  [`instructions/loader.py`](instructions/loader.py). Instructions **reference** facts;
  they never re-type a label or threshold — *unless the judgment itself
  originates here* (e.g. "$5k = real, not token pay"), in which case this is its
  authority and code/docs reference it.
- **Architecture principles & decisions → `docs/compass_concepts/`.**
  Human-readable end-state designs. Must not contradict code or instructions,
  but may lag in detail — they hold stable principles, not live values; they
  reference the operative source and define nothing operative.
- **Engineering guidance for agents working *on* the repo → `.agents/skills/` +
  root `AGENTS.md`.** Build-time, a different audience. Never conflate with
  `instructions/` (runtime, Compass's own agents).

This is the citations/states pattern (`artifacts/citations.py`,
`reference/states.py`) applied everywhere: when the authority changes, every
surface changes with it. Duplicated coverage/sort/count wording is the debt to
retire under this rule, not a pattern to extend.

## Authentication

Token prefixes (`pa_{env}_`) are cosmetic; hashes are checked in-DB. Admin
scope follows `compass.users.is_admin` for the token owner.
`api_keys.owner_email -> users.email` resolves per request, so scope changes
take effect immediately.

## Quality And Verdicts

`compass.verdicts.outcome` stores one of `VerdictOutcome`
([`db/rows.py`](db/rows.py), the authority) — `pass`, `fail`, or `error`. Operational
reports bucket these into product failures, skips, trace-missing, harness
errors, and contract-invalid — quote those axes separately, don't collapse
into one "errors" count. For scorecard or criterion changes, use
`/check-compass` (it holds the judge-tuning workflow — `replay-criterion`
before/after evidence on `judge_prompt` changes).

## Logfire

Use [../../docs/logfire-instrumentation-rules.md](../../docs/logfire-instrumentation-rules.md)
for instrumentation; `/logfire` for session/trace debugging.

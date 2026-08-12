# Narrative history: how Compass approached prompting

## The short version

Compass began as one tool-using assistant with one broad system prompt. It then separated *understanding the request* from *answering it*, added a concierge and a critic, split research from prose, and eventually moved most truth-bearing work out of prompts. The current design keeps a typed planner for interpretation, deterministic catalog/execution/rendering for facts, and a tightly bounded stylist for readable final prose.

The central lesson is not simply “use smaller prompts.” It is: give a model one bounded job, make important facts and decisions explicit in types and code, and use prompt text only where language judgment is actually needed.

## 1. December 2025 — one generalist agent

The first version was a single Pydantic AI agent with a single `SYSTEM_PROMPT`. It was asked to understand the question, choose tools, retrieve facts, reason about missing data, cite sources, state the school year, and write the answer. The prompt also listed available data and district names. See [the original extract](snapshots/2025-12-23-single-agent.md).

That was appropriate for a prototype, but it mixed identity, product scope, current database facts, tool behavior, formatting, and editorial voice. The district list is the early warning: a prompt cannot safely be the source of truth for a changing database.

## 2. January 2026 — intake, generation, concierge, and critic

The first structural change split the intake conversation from free-form answer generation. Intake resolved districts and metrics, clarified ambiguity, and emitted a typed `PolicyRequest`; generation used that request and tools to write the answer. The next iteration made a concierge the entry point for every turn, added structured conversation memory, and introduced a critic that graded the finished prose.

This improved multi-turn behavior and made quality visible, but models still held broad responsibilities. A later concierge version even received a large dynamic entity catalog. Caching helped latency, but it did not solve the authority problem: models were still asked to carry too much state and make decisions that ordinary code could make deterministically.

## 3. March–April 2026 — structured handoff and the Writer

The v2 architecture separated *research* from *writing*. A Generator returned a typed `ResearchPackage`: raw metric values, tables, summary statistics, coverage, citations, exports, and chart data. Its instruction was explicit: it must **never write prose**. Deterministic enrichment then added resolved citations and related material. A separate Writer received the enriched package and wrote the user-facing narrative.

On 2026-04-15 the Writer began returning a `WriterOutput` plus a response manifest. Deterministic validators checked that data claims traced to the package, cited IDs existed, requested districts were addressed, multi-district comparisons contained tables, and the manifest matched the body. The critic narrowed toward judgment questions such as tone and whether the answer met the user's intent. See [the Generator](snapshots/2026-03-26-structured-generator.md) and [Writer](snapshots/2026-04-15-structured-writer.md) extracts.

This established a rule that persists today: models can compose language, but values, citations, coverage, and required table shapes must be checked against structured artifacts.

## 4. May 2026 — typed planner, deterministic lanes, and no prose dispatch

The v3 proposal argued for deterministic responses when possible, a small planner when language interpretation was needed, a writer only for genuine synthesis, and a scoped critic rather than a universal review loop. The implemented path was incremental: a typed `PlannerTurn` chose a route and a `QueryPlan`; catalog resolution, execution, coverage logic, and rendering then did their own jobs.

The following cleanup deliberately removed phrase-matching overlays and hard-coded recovery prose from planning and rendering. It added typed concepts such as `requires_all_metrics`, `count_kind`, degree lanes, and threshold hints. Query-specific advice moved into selected planner-guidance snippets, while Python retained the selection rules.

The durable rule is: put a requirement in a type, catalog, validator, or renderer when it affects truth; keep instructions for the model's interpretation of the user's language.

## 5. May–July 2026 — packaged instructions and a sealed stylist

Static text moved out of Python into packaged Markdown and settled into the three folders used today: `model_instructions`, `planner_guidance`, and `answer_style_guides`. Dynamic context, models, tools, validators, retries, telemetry, and model selection stayed in Python.

The answer layer receives a **sealed answer brief** after planning, resolution, execution, validation, and deterministic rendering. The stylist can improve clarity and voice but may not add facts, values, citations, districts, coverage claims, or internal details. Immutable tables and source blocks must survive exactly. The writer became a constrained editor, not a second researcher.

Later changes were mostly small, evidence-backed corrections: a planner rule for a real ambiguous request, a selected guidance snippet for a known shape, a validator that prevents a bad claim, or deterministic presentation code that makes a disclosure survive the voice pass.

## Lessons for the next planner/writer work

1. **Shrink the initial prompt by moving facts, not merely deleting text.** Keep live catalog facts, IDs, values, coverage, citations, and source truth in the catalog/execution/rendering path.
2. **Use typed steps to make responsibilities legible.** Planner: interpret and emit a plan. Catalog: resolve terms. Executor: obtain rows. Renderer: state validated results. Stylist: improve prose without changing the answer.
3. **Keep dynamic context out of static Markdown.** Static assets should be reviewable and stable; runtime code should render typed context.
4. **Use snippets for narrow language patterns, not hidden routing logic.**
5. **Do not let the writer calculate or discover.** Give it a sealed, typed brief and retain deterministic fallback output.
6. **Treat a prompt change as product behavior.** Pair it with a literal scenario or focused regression and replay it.
7. **Prompt caching is a performance tool, not an architecture.** It does not make data-bearing prompt text safe or solve stale context.

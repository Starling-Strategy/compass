# Compass Prompt Assets

This package holds reviewable Compass prompt and prose guidance assets. Python
still owns runtime assembly, validators, typed contracts, deterministic
selection, and deterministic rendering. Markdown assets here are static text
that a model can consume.

## Layers

- `model_instructions/` contains static model-facing instructions passed into
  Pydantic AI agents or toolsets. Dynamic `@agent.instructions` functions stay
  in Python so they can render typed runtime dependencies, selected guidance,
  recognition reports, and authority warnings.
- `planner_guidance/` contains deterministic planner guidance selected by
  `planning/instruction_snippets.py`. Snippet metadata, trigger phrases, and
  priorities stay in Python because they are routing-adjacent control logic.
- `answer_style_guides/` contains user-facing answer guidance for the answer
  stylist. These prompts rewrite sealed answer briefs; they do not own data
  truth, coverage truth, citations, or policy authority.

## Still In Python

These pieces are not prompt assets at all and should not move into markdown:

- Pydantic output models and field descriptions
- route/result/coverage/source truth
- database IDs, metric IDs, district IDs, counts, values, and citations
- deterministic planner-guidance selection rules and trigger phrases
- runtime context builders such as prior-turn memory and answer briefs
- validators, retries, model settings, tools, hooks, and telemetry metadata
- deterministic renderer code that decides required rows, tables, caveats,
  source lists, and immutable blocks

## Review Rules

- Prompt assets may guide model behavior, but they must not contain drifting
  facts such as district counts, metric IDs, SQL, or catalog IDs (the
  no-hardcoded-facts rule, owned by backend [`AGENTS.md`](../AGENTS.md)
  guardrails 1–2).
- Pydantic model field descriptions stay near their fields and describe the
  structured contract. They are not a place for persona or answer voice.
- Renderer copy explains validated artifacts after execution. It should not
  leak into planner instructions.
- NCTQ policy markdown in `content/nctq-policy/` is source content, not prompt
  copy. Compass should surface it through governed policy-guidance paths.

## Loading

Use `load_model_instructions(filename)` for `model_instructions/*.md` and
`load_planner_guidance(filename)` for `planner_guidance/*.md`. Use
`load_answer_style_guide(filename)` or
`prompt_text("answer_style_guides", "default.md")` for answer guidance.
Loaders validate simple Markdown basenames, read packaged UTF-8 assets through
`importlib.resources`, and normalize with `.strip()`.

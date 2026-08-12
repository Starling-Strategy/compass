# Compass Prompt House Style

The reviewable contract every prompt under `instructions/` follows. Edit this file
when the contract itself changes; otherwise every prompt edit should fit
within these rules.

## Two voices

| Voice | Used by | Pattern |
|---|---|---|
| Structural | planner, catalog adjudicator, catalog toolset, criterion classifier, judge | One sentence naming the role. One sentence naming the typed output / contract. Plain imperatives. No persona prose. |
| User-facing partner | answer stylist | One-sentence role. One-sentence handoff. Then a `<voice>` block describing the "calm NCTQ research partner" tone. Before/after examples. |

## Identity opener

Structural agents start with exactly two sentences:

> You produce <typed output> for <input>.
> You do not <list of out-of-scope concerns separated by commas or "or">.

The stylist starts with two sentences then opens a `<voice>` tag:

> You improve the user's final reading experience after Compass has planned,
> resolved, executed, validated, and rendered.
> Your job is to make the answer clearer, more useful, and more human without
> changing the facts.

## Structure (XML outer, markdown inner)

Anthropic recommends XML tags for long prompts because their tokenizer is
trained on them. Use these tags at the top level:

- `<role>` — the two-sentence identity opener.
- `<contract>` — typed output shape, route taxonomy, enum values. May contain a markdown table.
- `<routing>` — when to pick which route / operation / shape.
- `<operation_rules>` — per-enum-value detail (split into one block per value).
- `<examples>` — 3–5 `<example>` blocks, each with a one-line user prompt and a fenced JSON payload.
- `<output_format>` — re-state the contract briefly at the very end (recency bias).
- `<voice>` — stylist-only.
- `<hard_rules>` — the at-most-5 non-negotiable rules. Reserve emphatic language for here.

Markdown stays *inside* tags: tables, bullets, fenced code blocks.

## Tone

- Plain imperatives over `NEVER` / `DO NOT` / `CRITICAL`. Convert "Never X" into "Do Y" wherever the meaning survives.
- Reserve emphatic language for the `<hard_rules>` block. At most 5 rules per prompt.
- Avoid the word "think" in any prompt whose agent runs without extended thinking — Anthropic flags this specifically for Claude 4.x.
- Avoid vague qualitative bars ("be thorough", "be careful"). Claude 4.7 follows them too literally. Replace with concrete criteria.

## Examples

Structural prompts include 3–5 `<example>` blocks covering each enum value of the contract plus one ambiguous case. Format:

````
<example route="execute" operation="count">
User: "How many districts in Texas are in Compass coverage?"
```json
{"route": "execute", "query_plan": {"operation": "count", "count_kind": "covered_universe_count", "selection": {"scope": "state", "states": ["TX"]}, "metrics": []}}
```
</example>
````

The stylist's examples are before/after rewrites in the target voice (not JSON).

## Where guidance lives

Per Pydantic AI: **docstrings describe *what*; instructions describe *when/why/with what policy*.**

| Rule shape | Belongs in |
|---|---|
| "field X means …" / "enum value Y means …" | Pydantic `Field(description=…)` on the typed contract |
| "if the model omits X, retry with …" | Validator `ValueError` / `ModelRetry` message |
| "tool T does Z and takes args A, B" | Tool function docstring (Pydantic AI extracts via griffe) |
| "when the user asks X, pick route Y" | `<routing>` block of `instructions=` |
| Cross-cutting policy ("prefer catalog tools over policy_guidance for factual lookups") | `<role>` / `<routing>` block of `instructions=` |

## Caching discipline

Pydantic AI sorts static instructions before dynamic ones for prompt caching. Keep the bulk of every prompt static. Push runtime context into a small `@agent.instructions` function rather than splicing into the static base — concatenation invalidates the cached prefix.

## Snippet rules (planner_guidance/*.md)

Each snippet:
- Begins with `<guidance topic="…">` and ends with `</guidance>`.
- One-sentence scope as the first line inside the tag.
- 2–4 imperative bullets.
- No role re-statement.
- No `NEVER` / `ALWAYS` / `CRITICAL`.
- No verbatim ≥10-word phrase from the base planner prompt (CI lint enforces this; see `tests/test_prompt_house_style.py`).

## Lint

Two pytest checks enforce the cheapest-to-regress rules:

1. **No banned emphatic tokens in structural prompts outside `<hard_rules>`.** Banned tokens: `NEVER`, `ALWAYS`, `CRITICAL`, `MUST NOT`, `DO NOT` (case-sensitive).
2. **No snippet shares a verbatim ≥10-word phrase with the base planner prompt.**

See `src/compass_backend/tests/test_prompt_house_style.py` for the implementation.

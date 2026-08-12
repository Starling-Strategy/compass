# Compass Prompt Assets

This folder holds markdown that Compass loads into Pydantic AI `instructions`
or the answer-stylist prompt. It is for reviewable wording, not product logic.

## Folders

- `model_instructions/` - stable instructions for active model-facing steps:
  planner, catalog adjudicator, catalog toolset, criterion classifier, and
  judge.
- `planner_guidance/` - small planner guidance snippets. Python decides when a
  snippet applies; markdown only holds the instruction body.
- `answer_style_guides/` - user-facing answer style guidance for rewriting a
  sealed answer brief.

## Rules

- Every prompt follows [`HOUSE_STYLE.md`](HOUSE_STYLE.md). The two pytest
  lints in `src/compass_backend/tests/test_prompt_house_style.py` enforce
  the cheapest-to-regress rules.
- Do not put district IDs, metric IDs, counts, values, citations, SQL, coverage
  truth, or source truth in prompt assets.
- Keep `deps_type`, tools/toolsets, output models, validators, retries, hooks,
  model settings, and telemetry in Python.
- Do not add future-agent folders until the runtime call site is wired to load
  them.
- If a prompt edit changes behavior, include the scenario, test, or before/after
  sample that proves the intended change.

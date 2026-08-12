# Compass prompt and instruction history

This is a research guide to how Compass has used prompts and instructions since the Policy Advisor repository began. It is deliberately a guide to source history, not a second prompt package. The live assets remain in [`backend/src/compass_backend/instructions/`](../../../backend/src/compass_backend/instructions/); copying every large revision here would create a second source that could drift.

Read [the narrative history](history.md) first. It explains the architectural changes, what each prompt was allowed to do, and the lessons that still matter when changing the planner or writer.

## What is saved here

- `snapshots/` preserves selected historical prompt text from the major designs.
- [source-index.md](source-index.md) identifies the original repository, commit, and source path for the full historical prompt versions.

The source history begins at `8040238f` (2025-12-23). This Compass repository is a curated production snapshot; [PROVENANCE.md](../../../PROVENANCE.md) records the source repository and production commit for its vendored backend.

## Current prompt map

Today the static text lives in three reviewable folders:

- `model_instructions/`: planner, catalog, classifier, judge, and clarify-stylist instructions.
- `planner_guidance/`: small, deterministically selected planning recipes. Python owns their trigger rules and priority.
- `answer_style_guides/`: the final answer-stylist voice prompt.

Runtime context, Pydantic output types, tools, validators, retries, model settings, and rendering remain in Python. Prose may guide a model, but it may not become the authority for facts or workflow control.

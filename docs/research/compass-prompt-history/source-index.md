# Source index: full prompt versions

Compass is a curated production snapshot. The historical prompt sources below live in the original `Starling-Strategy/policy-advisor` repository, which is **private** — the commit links are recorded for internal traceability and will not resolve for public readers. The prompts that matter are extracted in full into the snapshots linked from this page.

## Selected historical extracts copied here

| Date | Commit | Design | Extract |
| --- | --- | --- | --- |
| 2025-12-23 | [`8040238f`](https://github.com/Starling-Strategy/policy-advisor/blob/8040238f/src/policy_advisor/agent.py) | One generalist system prompt | [single agent](snapshots/2025-12-23-single-agent.md) |
| 2026-03-26 | [`e3c3f6bc`](https://github.com/Starling-Strategy/policy-advisor/blob/e3c3f6bc/src/policy_advisor/agents/generation.py) | Structured research generator | [generator](snapshots/2026-03-26-structured-generator.md) |
| 2026-04-15 | [`d5b45d7e`](https://github.com/Starling-Strategy/policy-advisor/blob/d5b45d7e/src/compass_agents/concierge.py) | Writer with a manifest and validators | [Writer](snapshots/2026-04-15-structured-writer.md) |

## Full later assets

| Date | Commit | Exact source | Why it matters |
| --- | --- | --- | --- |
| 2026-04-27 | [`b22e459c`](https://github.com/Starling-Strategy/policy-advisor/blob/b22e459c/src/compass_v3/v3-architecture.md) | v3 architecture | Proposed lanes, artifact-first writing, and scoped criticism. |
| 2026-05-06 | [`abae3e24`](https://github.com/Starling-Strategy/policy-advisor/blob/abae3e24/src/compass_v3/agents/planner.py) | First typed planner | Planning became a typed model output rather than prose. |
| 2026-05-18 | [`e13ab67e`](https://github.com/Starling-Strategy/policy-advisor/blob/e13ab67e/src/compass_backend/planning/instruction_snippets.py) | Selected snippets | Retry advice moved out of generic planner text. |
| 2026-05-26 | [`50b92b91`](https://github.com/Starling-Strategy/policy-advisor/tree/50b92b91/src/compass_backend/prompts) | First packaged assets | Full planner prompt and snippets moved from Python into Markdown. |
| 2026-05-27 | [`bad071f7`](https://github.com/Starling-Strategy/policy-advisor/blob/bad071f7/src/compass_backend/prompts/copy/answer_layer.md) | First answer stylist | The sealed-brief answer layer became active. |
| 2026-05-28 | [`17ed5036`](https://github.com/Starling-Strategy/policy-advisor/tree/17ed5036/src/compass_backend/prompts) | Clear asset hierarchy | Explicit split between model instructions, planner guidance, and style. |
| 2026-05-28 | [`fd0a9482`](https://github.com/Starling-Strategy/policy-advisor/blob/fd0a9482/src/compass_backend/prompts/answer_style_guides/default.md) | Plain-Spoken Explainer | Style rules and the sealed-answer contract were strengthened. |
| 2026-06-08 | [`ac984608`](https://github.com/Starling-Strategy/policy-advisor/tree/ac984608/src/compass_backend/instructions) | Current folder name | `prompts/` became `instructions/`. |
| 2026-07-15 | [`a22db13e`](https://github.com/Starling-Strategy/policy-advisor/tree/a22db13e/src/compass_backend/instructions) | Source-repository baseline | Latest version examined during the original history review. |

The live, vendored production assets in this repository are under [`backend/src/compass_backend/instructions/`](../../../backend/src/compass_backend/instructions/). Do not treat historical files as active runtime imports.

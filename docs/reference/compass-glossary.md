# Compass glossary

The shared vocabulary for this documentation set. Definitions are short on
purpose; the last column names the one page that carries the full detail.

## Product and platform

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Compass** | NCTQ's AI research assistant for questions about reviewed U.S. school-district policy data. | [Start Here](../01-start-here.md) |
| **NCTQ** | The National Council on Teacher Quality, the organization whose reviewed policy data and published positions Compass uses. | [nctq.org](https://www.nctq.org/) |
| **NCTQ.ai platform** | The broader product platform around Compass: the public chat frontend, the Policy Advisor API, the staff Dashboard, and the supporting data systems. | [Start Here](../01-start-here.md#compass-in-one-minute) |
| **District Policy Pathfinder** | The NCTQ website experience where the public Compass chat is embedded. | [Pathfinder integration](../08-technical-reference.md#pathfinder-integration) |
| **Policy Advisor API** | Compass's backend service — the name used for it in code, deployment, and this documentation. It plans requests, reads approved data, builds answers, and streams responses. | [API reference](../08-technical-reference.md#api-reference) |
| **Compass Frontend** | The public PHP/Apache web application that presents the chat and proxies browser requests to the API. | [Application layout](../08-technical-reference.md#application-layout-and-runtime-shape) |
| **NCTQ Dashboard** | The internal FastHTML application for staff review, analytics, quality results, and data inventory. | [Administration and Dashboard](../05-administration-and-dashboard.md) |
| **`compass` schema** | The canonical PostgreSQL schema holding the runtime views, source data, enrichment, and sync/evaluation records Compass uses. | [Schema reference](compass-schema.md) |

## Data and coverage

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **TCD** | The NCTQ source system named throughout the code. It supplies district, topic, metric, answer, citation, and source records to the Compass data pipeline. Its public product name is the District Policy Pathfinder. | [Where the data comes from](../03-data-and-databricks.md#where-the-data-comes-from) |
| **Databricks** | The Azure data-preparation platform that runs the scheduled jobs which clean, validate, and publish data for Compass. | [The nightly pipeline](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Bronze / Silver / Gold** | The three pipeline stages before production: an exact copy of the source; cleaned, deduplicated, typed data; then the exact shape the applications expect. | [The nightly pipeline](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Validation gate** | The checks that must pass before a data push reaches production: row counts, nulls, uniqueness, and schema shape. A failure stops the whole run. | [The nightly pipeline](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Sync** | One scheduled movement of source data through the pipeline into the Compass database. Each run records its status and what it processed. | [Sync and audit tables](compass-schema.md#sync-and-audit-tables) |
| **District** | A school district in the NCTQ policy catalog, usually joined to federal NCES context. | [`district_profiles`](compass-schema.md#compassdistrict_profiles) |
| **Topic / subtopic** | A broad policy area in the NCTQ catalog (leave, salary, evaluation), and a more specific grouping inside it. | [`navigator_topics`](compass-schema.md#compassnavigator_topics) |
| **Metric** | A named policy measure or question Compass can look up, compare, count, or otherwise query. | [`navigator_metrics`](compass-schema.md#compassnavigator_metrics) |
| **Policy answer** | A reviewed value for one district, metric, and academic year, stored with what's needed to interpret and cite it. | [`navigator_answers`](compass-schema.md#compassnavigator_answers) |
| **Source document** | A contract, handbook, board policy, or other reviewed document behind a policy answer. | [`navigator_sources`](compass-schema.md#compassnavigator_sources) |
| **Citation** | The recorded relationship connecting an answer to the document or approved source supporting it. | [Citations](../02-product-and-answer-flow.md#citations) |
| **Citation marker** | The visible marker in a Compass answer that links a table cell or claim to its supporting source. | [Citations](../02-product-and-answer-flow.md#citations) |
| **Academic year** | The school year a policy value belongs to, stored canonically as `2024 - 2025`. It is part of the data's meaning, not a display label. | [What "current" means](../03-data-and-databricks.md#what-current-means) |
| **NCES** | The National Center for Education Statistics. Compass uses selected NCES district context — locale, enrollment, staffing, finance — with its source vintages labeled. | [NCES enrichment tables](compass-schema.md#nces-enrichment-tables) |
| **Coverage state** | Whether a requested district/topic/year cell has usable reviewed data. Compass distinguishes `covered`, `issue not addressed`, `not applicable`, `not reviewed`, and `out of universe`. | [What Compass covers](../03-data-and-databricks.md#what-compass-covers) |
| **Closed system** | The rule that a chat turn reads approved `compass` data and managed content rather than browsing the open web. | [Data & the Databricks Platform](../03-data-and-databricks.md#the-short-version) |
| **Runtime view** | A stable, denormalized database view the chat path reads: `district_profiles`, `policy_questions`, `policy_answers`, and `answer_sources`. They are materialized, so the application gets a fixed read shape without repeating the upstream joins on every request. | [Runtime materialized views](compass-schema.md#runtime-materialized-views) |

## Answer flow

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Grounded answer** | An answer whose material values and citations come from data retrieved for that turn, rather than from a model's own words. | [The short version](../02-product-and-answer-flow.md#the-short-version) |
| **Planner** | The model-backed stage that interprets the request and produces a typed plan, or picks a non-data route such as clarify, policy guidance, publication, or direct reply. | [Planning](../02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan) |
| **Typed plan** | A structured record of the intended route, entities, filters, operation, and output shape. Downstream code reads its fields instead of parsing prose. | [Planning](../02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan) |
| **Route** | The typed path chosen for a turn: `execute`, `clarify`, `policy_guidance`, `publication`, or `direct`. | [Planning](../02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan) |
| **Catalog resolver** | The deterministic layer that exchanges user phrases for approved district, topic, and metric identifiers before any data is fetched. | [Retrieval](../02-product-and-answer-flow.md#retrieval-phrases-become-verified-entities) |
| **Executor** | The deterministic query layer that runs an approved typed operation against the `compass` schema and returns structured results. | [Generation](../02-product-and-answer-flow.md#generation-facts-first-phrasing-second) |
| **Renderer** | Ordinary code that assembles the lead, tables, citations, coverage notes, and export artifacts from validated results. No model is involved. | [Generation](../02-product-and-answer-flow.md#generation-facts-first-phrasing-second) |
| **Sealed brief** | The finished answer skeleton handed to the answer stylist. Its factual sections and citation blocks are immutable inputs to the rewrite. | [Generation](../02-product-and-answer-flow.md#generation-facts-first-phrasing-second) |
| **Answer stylist** | An optional model-backed editor that improves wording after the facts, tables, caveats, and citations are assembled. It cannot add or change a fact or citation. | [Generation](../02-product-and-answer-flow.md#generation-facts-first-phrasing-second) |

## Quality and evaluation

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Quality dimension** | One lens used to judge an answer: selection, data fidelity, coverage-state labeling, filtering, sorting, citation, or consistency. Voice and tone is a separate cross-cutting lens. | [What we mean by quality](../04-quality-and-evaluation.md#1-what-we-mean-by-quality) |
| **Scenario** | A user goal or behavior family the team wants Compass to handle reliably. | [The saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Case** | A runnable instance of a scenario: the literal prompt, needed context, expected behavior, and data assumptions. | [The saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Criterion** | One checkable rule for a case, such as applying the requested year filter or attaching a supporting citation to every displayed value. | [The saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Verdict** | The recorded result of evaluating one answer against one criterion. | [The saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Sweep** | A named evaluation run that replays a set of cases and writes its verdicts to the ledger. | [Saved-case sweeps](../04-quality-and-evaluation.md#saved-case-sweeps-and-targeted-replay) |
| **Evaluation ledger** | The append-only record of scenarios, cases, criteria, verdicts, and sweep runs. | [The saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Scorecard** | A roll-up of verdicts by dimension, scenario, case, and run. A signal for investigation, not proof that every untested question is safe. | [Reading the scorecard](../04-quality-and-evaluation.md#4-how-the-scorecard-should-be-read) |
| **Regression case** | A saved case kept because it exposed a failure or guards a fix. The prompt, or a faithful equivalent, stays in the library. | [From a failure to a fix](../04-quality-and-evaluation.md#6-from-a-failure-to-a-fix) |

## Related terms

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Prompt / instruction** | Text that guides a model's interpretation, bounded judgment, or voice. Truth-bearing rules live in typed contracts and ordinary code instead. | [Instruction inventory](prompt-and-model-inventory.md#complete-instruction-asset-inventory) |
| **Planner guidance** | A small, topic-specific instruction snippet selected deterministically for a question, capped at three per turn. It carries no execution, catalog, or citation authority. | [On-demand planner guidance](prompt-and-model-inventory.md#on-demand-planner-guidance) |
| **Guardrail** | An explicit behavioral rule about what Compass must not claim or must always disclose, as distinct from voice guidance. Some are enforced by validators, others by instruction only. | [Guardrails](../02-product-and-answer-flow.md#guardrails-what-compass-must-not-say-and-what-it-must-always-say) |
| **Canonical coverage string** | A sentence reproduced verbatim to mark one specific coverage state. Rewording one blurs a distinction the reviewed data actually makes. | [Required disclosures](../02-product-and-answer-flow.md#disclosures-compass-is-required-to-include) |
| **Chat users vs. unique visitors** | Two different dashboard counts. Chat users is `COUNT(DISTINCT visitor_id)` over conversations; unique visitors is site-level Umami traffic that de-duplicates only within a calendar month. They do not reconcile and should not be compared as a ratio. | [Key metrics](../05-administration-and-dashboard.md#key-metrics-and-how-they-are-calculated) |
| **Pathfinder embed** | The iframe integration placing the Compass Frontend inside the NCTQ Pathfinder, including `?embed=true` and the `postMessage` contract. | [Pathfinder integration](../08-technical-reference.md#pathfinder-integration) |
| **Metric Calculator** | NCTQ's internal analyst-review tool, and its upstream prediction engine PiedPiper, for turning district policy documents into reviewed data. Not part of Compass; approved answers reach Compass only indirectly, through the TCD/Pathfinder database. | [Metric Calculator reference](metric-calculator.md) |

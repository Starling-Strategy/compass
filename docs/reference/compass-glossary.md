# Compass glossary

This glossary gives the documentation set a shared vocabulary. The definitions are
short on purpose. Follow the links for the implementation detail, current data
inventory, or field-level meaning.

## Product and platform

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Compass** | NCTQ's AI research assistant for questions about reviewed U.S. school-district policy data. | [Start Here](../01-start-here.md), [Product & Answer Flow](../02-product-and-answer-flow.md) |
| **NCTQ** | The National Council on Teacher Quality, the organization whose reviewed policy data and published positions Compass uses. | [NCTQ](https://www.nctq.org/) |
| **NCTQ.AI platform** | The broader product platform around Compass: the public chat frontend, Policy Advisor API, staff dashboard, and supporting data systems. | [Start Here](../01-start-here.md#compass-in-one-minute), [Technical Reference](../08-technical-reference.md#application-layout-and-runtime-shape) |
| **District Policy Pathfinder** | The NCTQ website experience where the public Compass chat is embedded. | [District Policy Pathfinder](https://www.nctq.org/district-policy-pathfinder/), [Pathfinder integration](../08-technical-reference.md#pathfinder-integration) |
| **Policy Advisor API** | The original or alternate name for Compass's backend API. It is the service that plans requests, reads approved data, builds answers, and streams responses. | [Technical Reference API](../08-technical-reference.md#api-reference) |
| **Frontend** | The public PHP/Apache web application that presents the chat and proxies browser requests to the API. | [Technical Reference](../08-technical-reference.md#application-layout-and-runtime-shape) |
| **NCTQ dashboard** | The internal FastHTML application for staff review, analytics, quality results, and data inventory. | [Quality & Evaluation](../04-quality-and-evaluation.md), [Technical Reference](../08-technical-reference.md#application-layout-and-runtime-shape) |
| **`compass` schema** | The canonical PostgreSQL schema that holds the runtime views, source data, enrichment, and sync/evaluation records used by Compass. | [Schema reference](compass-schema.md), [Data & Databricks](../03-data-and-databricks.md#the-data-dictionary) |

## Data and coverage

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **TCD** | The NCTQ source/catalog system named in the repository. It supplies district, topic, metric, answer, citation, and source records to the Compass data pipeline. | [Schema reference](compass-schema.md#source-and-policy-data-tables), [Data sources](../03-data-and-databricks.md#where-the-data-comes-from) |
| **Databricks** | The Azure data-preparation platform that runs the scheduled jobs which clean, validate, and publish data for Compass. | [Data & Databricks](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Bronze** | The pipeline stage that keeps an exact copy of incoming source data before cleanup. | [Nightly pipeline](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Silver** | The pipeline stage where data is cleaned, deduplicated, joined, typed, and checked against business rules. | [Nightly pipeline](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Gold** | The pipeline stage shaped for the applications' expected tables and fields. | [Nightly pipeline](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Production** | The validated database layer that the running Compass applications read. In the public schema docs, this usually means the PostgreSQL `compass` schema and its runtime views. | [Data & Databricks](../03-data-and-databricks.md#the-nightly-pipeline) |
| **Sync** | A scheduled movement of source data through the pipeline into the Compass database. A sync run records its status and what it processed. | [Sync and audit tables](compass-schema.md#sync-and-audit-tables) |
| **Validation gate** | The checks that must pass before a data push can reach production, including row counts, nulls, uniqueness, and schema shape. | [The nightly pipeline](../03-data-and-databricks.md#the-nightly-pipeline) |
| **District** | A school district represented in the NCTQ policy catalog, often joined to federal NCES context. | [Schema reference](compass-schema.md#compassdistrict_profiles), [Data coverage](../03-data-and-databricks.md#what-compass-covers) |
| **Topic** | A broad policy area in the NCTQ catalog, such as leave, salary, or evaluation. | [Schema reference](compass-schema.md#compassnavigator_topics) |
| **Subtopic** | A more specific grouping inside a topic. | [Schema reference](compass-schema.md#compassnavigator_topics) |
| **Metric** | A named policy measure or question that Compass can look up, compare, count, or otherwise query when supported. | [Schema reference](compass-schema.md#compassnavigator_metrics) |
| **Policy answer** | A reviewed value for a district, metric, and academic year, stored with the information needed to interpret and cite it. | [Schema reference](compass-schema.md#compassnavigator_answers) |
| **Source document** | A contract, handbook, board policy, or other reviewed document behind a policy answer. | [Schema reference](compass-schema.md#compassnavigator_sources), [Citations](../02-product-and-answer-flow.md#citations) |
| **Citation** | The source relationship that connects an answer or claim to the document or approved source supporting it. | [Schema reference](compass-schema.md#compassnavigator_citations), [Citations](../02-product-and-answer-flow.md#citations) |
| **Citation marker** | The visible marker in a Compass answer that links a table cell or claim to its supporting source. | [Citations](../02-product-and-answer-flow.md#citations) |
| **Academic year** | The school year attached to a policy value, such as `2024-25`. It is part of the meaning of the data, not just a display label. | [What current means](../03-data-and-databricks.md#what-current-means), [Schema reference](compass-schema.md#compasspolicy_answers) |
| **NCES** | The National Center for Education Statistics. Compass uses selected NCES district context, such as locale, enrollment, staffing, and finance fields, with source vintages labeled. | [Data sources](../03-data-and-databricks.md#where-the-data-comes-from), [NCES tables](compass-schema.md#nces-enrichment-tables) |
| **Coverage state** | The status that explains whether a requested district/topic/year cell has usable reviewed data. Compass distinguishes `covered`, `issue not addressed`, `not applicable`, `not reviewed`, and `out of universe`. | [What Compass covers](../03-data-and-databricks.md#what-compass-covers) |
| **Closed system** | The rule that a chat turn reads from the approved `compass` data and managed content rather than browsing the open web. | [Data & Databricks](../03-data-and-databricks.md#the-short-version) |
| **Runtime view** | A stable, query-friendly database view used by the chat path. Compass's main runtime views include `district_profiles`, `policy_questions`, `policy_answers`, and `answer_sources`. | [Runtime materialized views](compass-schema.md#runtime-materialized-views) |
| **Materialized view** | A database view whose results are stored and refreshed, giving the application a stable read shape without repeating the full upstream joins on every request. | [Runtime materialized views](compass-schema.md#runtime-materialized-views) |

## Answer flow

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Grounded answer** | An answer whose material values and citations come from approved data or content retrieved for that turn, rather than being invented by a model. | [Product & Answer Flow](../02-product-and-answer-flow.md#the-short-version) |
| **Planner** | The model-backed stage that interprets the user's request and produces a typed plan or chooses a non-data route such as clarify, policy guidance, publication, or direct reply. | [Planning](../02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan) |
| **Typed plan** | A structured representation of the intended route, entities, filters, operation, and output shape. Downstream code uses its fields instead of parsing prose. | [Planning](../02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan) |
| **Catalog resolver** | The deterministic layer that turns user phrases into approved district, topic, metric, and other catalog identifiers before data is fetched. | [Retrieval](../02-product-and-answer-flow.md#retrieval-phrases-become-verified-entities) |
| **Executor** | The deterministic query layer that runs an approved typed operation against the `compass` schema and returns structured results. | [Generation](../02-product-and-answer-flow.md#generation-facts-first-phrasing-second) |
| **Renderer** | Ordinary code that assembles the lead, tables, citations, coverage notes, and export artifacts from validated results. | [Generation](../02-product-and-answer-flow.md#generation-facts-first-phrasing-second) |
| **Answer stylist** | An optional model-backed editor that improves wording after the facts, tables, caveats, and citations have been assembled. It cannot add facts or citations. | [Prompts and instructions](../02-product-and-answer-flow.md#prompts-and-instructions-where-they-live-how-they-work) |
| **Sealed brief** | The validated answer skeleton given to the answer stylist. Its factual sections and citation blocks are treated as immutable inputs to the rewrite. | [Generation](../02-product-and-answer-flow.md#generation-facts-first-phrasing-second) |
| **Route** | The typed path Compass chooses for a turn, such as `execute`, `clarify`, `policy_guidance`, `publication`, or `direct`. | [Planning](../02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan) |

## Quality and evaluation

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Scenario** | A user goal or behavior family that the team wants Compass to handle reliably. | [Saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Case** | A runnable instance of a scenario, including the literal prompt, needed context, expected behavior, and data assumptions. | [Saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Criterion** | One checkable rule for a case, such as applying the requested year filter or attaching a supporting citation to every displayed value. | [Saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Verdict** | The recorded result of evaluating one answer against one criterion. | [Saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library) |
| **Sweep** | A named evaluation run that replays a set of cases and writes its verdicts to the evaluation ledger. | [Saved-case sweeps](../04-quality-and-evaluation.md#saved-case-sweeps-and-targeted-replay) |
| **Scorecard** | A roll-up of verdicts by dimension, scenario, case, and run. It is a signal for investigation, not proof that every untested question is safe. | [Reading the scorecard](../04-quality-and-evaluation.md#4-how-the-scorecard-should-be-read) |
| **Regression case** | A saved case kept because it exposed a failure or guards a fix. The prompt or a faithful equivalent remains in the test library. | [From a failure to a fix](../04-quality-and-evaluation.md#6-from-a-failure-to-a-fix) |
| **Evaluation ledger** | The append-only record of scenarios, cases, criteria, verdicts, and sweep runs. | [Saved scenario library](../04-quality-and-evaluation.md#2-the-saved-scenario-library), [Schema reference](compass-schema.md) |
| **Quality dimension** | One lens used to judge an answer, including selection, data fidelity, coverage-state labeling, filtering, sorting, citation, and consistency. Voice and tone is a separate cross-cutting lens. | [What quality means](../04-quality-and-evaluation.md#1-what-we-mean-by-quality) |

## Related terms

| Term | Plain-language meaning | Read more |
| --- | --- | --- |
| **Prompt and instruction** | Text that guides a model's interpretation, bounded judgment, or voice. Truth-bearing rules belong in typed contracts and ordinary code when possible. | [Prompt and instruction history](../research/compass-prompt-history/README.md), [Technical Reference](../08-technical-reference.md#prompts-and-instruction-references) |
| **Open-source dependency** | A third-party runtime, library, or tool that Compass uses under its own upstream license. The curated credits live in the technical reference; manifests and lockfiles are the complete inventory. | [Open-source acknowledgements](../08-technical-reference.md#license-and-open-source-acknowledgements) |
| **Pathfinder embed** | The iframe integration that places the Compass frontend inside the NCTQ Pathfinder, including `?embed=true` and the `postMessage` contract. | [Pathfinder integration](../08-technical-reference.md#pathfinder-integration) |

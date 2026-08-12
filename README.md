# Compass

Compass is an AI research assistant for U.S. school-district policy, built for the
[National Council on Teacher Quality (NCTQ)](https://www.nctq.org). It answers
questions about district policies (salaries, leave, evaluation, staffing, and more)
with every value grounded in NCTQ's reviewed policy data and cited back to its source
documents. Compass powers the chat experience on NCTQ's
[District Policy Pathfinder](https://www.nctq.org/district-policy-pathfinder/).

This repository holds the production code for the three Compass applications and the
public documentation set.

| Application | What it is |
| --- | --- |
| `src/compass_backend` | The Policy Advisor API: the chat engine (Python / FastAPI) |
| `src/compass_frontend` | The public chat web app (PHP / Apache) |
| `src/nctqai` | The internal review-and-analytics dashboard (Python / FastHTML) |

> **Status:** documentation is landing first; the application code is being seeded
> from the production deploy branches and will follow.

## Documentation

The `/docs` folder is the documentation set. Sections are numbered against the master
outline; sections 5-7 (administration, hosting and security, costs and accounts)
describe internal operations and are maintained privately, which is why the numbering
here has gaps.

| # | Doc | Covers | Status |
| --- | --- | --- | --- |
| 1 | [Start Here & Glossary](docs/01-start-here.md) | What Compass is, how to read these docs, glossary | Stub |
| 2 | [Product & Answer Flow](docs/02-product-and-answer-flow.md) | How a question becomes a grounded, cited answer: planning, retrieval, generation, verification, prompts, voice | **Draft for review** |
| 3 | [Data & the Databricks Platform](docs/03-data-and-databricks.md) | What data Compass covers, where it comes from, the schema, how it stays current | **Draft for review** |
| 4 | [Quality & Evaluation](docs/04-quality-and-evaluation.md) | The quality dimensions, the scenario library, how accuracy is measured | Stub |
| 8 | [Technical Reference](docs/08-technical-reference.md) | Licensing, stack, API endpoints, configuration, Pathfinder embed | Stub |
| 9 | [Known Issues & Limitations](docs/09-known-issues-and-limitations.md) | What's broken, worked around, or out of scope, honestly stated | Stub (first entry drafted) |

Writing conventions for every section: a plain-language overview first, then a body
that assumes technical fluency; each fact lives in exactly one doc and is linked from
the others; diagrams are Mermaid so they render on GitHub and diff in review.

## License

[MIT](LICENSE) © National Council on Teacher Quality.

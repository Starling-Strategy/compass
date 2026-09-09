# Compass

Compass is an AI research assistant for U.S. school-district policy, built for the
[National Council on Teacher Quality (NCTQ)](https://www.nctq.org). It answers
questions about district policies (salaries, leave, evaluation, staffing, and more)
with every value grounded in NCTQ's reviewed policy data and cited back to its source
documents. Compass powers the chat experience on NCTQ's
[District Policy Pathfinder](https://www.nctq.org/district-policy-pathfinder/).

This repository holds the production code for the three Compass applications and the
documentation set.

| Application | What it is |
| --- | --- |
| [`backend/`](backend/) | The Policy Advisor API: the chat engine (Python / FastAPI) |
| [`frontend/`](frontend/) | The public chat web app (PHP / Apache) |
| [`dashboard/`](dashboard/) | The internal review-and-analytics dashboard (Python / FastHTML) |

Each directory has its own Dockerfile. Azure DevOps also retains deployment
scaffolding; do not assume every local Dockerfile is the live pipeline build.
[PROVENANCE.md](PROVENANCE.md) records the historical code import, not current
deployment status.

## Contributing and deployment

Start with [AGENTS.md](AGENTS.md) for setup, build/test commands, code conventions,
security boundaries, and PR approval rules. The single current release runbook is
[Hosting, Deployment, and Security](docs/06-hosting-deployment-security.md): it
explains the GitHub `main` → Azure DevOps mirror/queue → Azure production path
and separates verified release evidence from unverified staging history.
Merging to `main` requires explicit user approval. Root/docs-only changes do not
trigger application sync; tests or docs inside application folders do.

## Documentation

The `/docs` folder is the documentation set. Sections are numbered against the master
outline. Sections 5-7 (administration, hosting and security, and account ownership)
are operational handoff documentation for NCTQ; the rest describe the product itself.

| # | Doc | Covers |
| --- | --- | --- |
| 1 | [Start Here](docs/01-start-here.md) | What Compass is, FAQ, how to read these docs, and glossary entry point |
| 2 | [Product & Answer Flow](docs/02-product-and-answer-flow.md) | How a question becomes a grounded, cited answer: planning, retrieval, generation, verification, prompts, voice |
| 3 | [Data & the Databricks Platform](docs/03-data-and-databricks.md) | What data Compass covers, where it comes from, the schema, how it stays current |
| 4 | [Quality & Evaluation](docs/04-quality-and-evaluation.md) | The quality dimensions, the scenario library, how accuracy is measured |
| 5 | [Administration and Dashboard](docs/05-administration-and-dashboard.md) | The staff Dashboard's purpose, audience, and how NCTQ monitors conversations and quality results |
| 6 | [Hosting, Deployment, and Security](docs/06-hosting-deployment-security.md) | Current Azure release runbook; local development, historical/unverified Coolify staging, security, and recovery |
| 7 | [Costs, Accounts, and Budget](docs/07-costs-accounts-and-budget.md) | External account ownership checklist plus Azure and model cost/budget planning |
| 8 | [Technical Reference](docs/08-technical-reference.md) | Licensing, stack, API endpoints, configuration, Pathfinder embed |
| 9 | [Known Issues & Limitations](docs/09-known-issues-and-limitations.md) | What's broken, worked around, or out of scope, honestly stated |

Writing conventions for every section: a plain-language overview first, then a body
that assumes technical fluency; each fact lives in exactly one section, and a
cross-reference points there only when a reader genuinely needs to go; diagrams are
Mermaid so they render on GitHub and diff in review.

Four reference documents sit under `docs/reference/`: the
[system architecture](docs/reference/architecture.md) for the cross-system view,
the [schema reference](docs/reference/compass-schema.md) for field-level detail,
the [glossary](docs/reference/compass-glossary.md) for shared vocabulary, and the
[prompt and model inventory](docs/reference/prompt-and-model-inventory.md) for
model roles and their guardrails.

## License

[MIT](LICENSE) © National Council on Teacher Quality.

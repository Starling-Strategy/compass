# 7. Costs, Accounts, and Budget

This section explains which accounts keep Compass running, who is expected to
own and pay for them, and how to refresh the budget. The figures below are a
dated operating baseline, not a current invoice. Keep Azure hosting, model
usage, and shared services separate so a change in one does not hide a change in
another.

## Account and subscription inventory

| Account or service | Purpose | Owner and billing | Status or decision |
| --- | --- | --- | --- |
| Microsoft Azure Sponsorship subscription | Production applications, PostgreSQL, Databricks, Data Factory, email, networking, registries, and logs in Central US | NCTQ owns the Azure subscription and its billing relationship | Known decision. Sponsorship credits can reduce the amount invoiced, but do not reduce measured consumption. Confirm the current billing administrator during handoff. |
| NCTQ Airtable | Source catalog for chatbot-ready NCTQ publications | NCTQ owns the Airtable account and subscription | Known decision. Confirm the operational base owner and backup owner. |
| Pydantic AI Gateway | Routes Compass model calls to Anthropic and records estimated request cost | Billing owner is not identified in the supplied handoff material | Existing setup. Assign an NCTQ billing owner or record the approved continuing arrangement before transfer. |
| Pydantic Logfire | Application traces, model usage, and request cost estimates | Account and billing owner are not identified in the supplied handoff material | Existing setup. Confirm organization administrators, plan, retention, and billing treatment. |
| Anthropic models through the Pydantic AI Gateway | Planner, bounded catalog adjudication, and model-judged quality checks | Charged through the gateway or its configured provider arrangement, not Azure | Active dependency. Confirm the provider billing path and spend cap in the Gateway organization settings. |
| GitHub, Starling Strategy organization | Canonical source repository and issue history for the active Compass codebase | Current custody is the Starling Strategy GitHub organization | Known custody location. Final long-term custody, NCTQ access, and backup or transfer procedure require an explicit written decision. |
| Azure DevOps organization `nctqai` | Production build pipelines and mirrored deployment branches | NCTQ Azure and Azure DevOps access is the target operational ownership | Existing production setup. Confirm named administrators and pipeline service-connection owners. |
| Google Analytics | Pathfinder page visitor measurement used by the dashboard | Owner and billing treatment are not identified in the supplied handoff material | Confirm the NCTQ property administrator and service-account custodian. |
| Shared operational services | 1Password, monitoring, alert delivery, and any team tooling used across clients | May be shared rather than Compass-specific | Do not charge the full subscription to Compass without a documented allocation rule. |

The application and data architecture behind these accounts is summarized in
[Start Here](01-start-here.md#how-the-parts-fit-together). The current data
sources, including NCTQ Airtable, are described in
[Data and the Databricks Platform](03-data-and-databricks.md#where-the-data-comes-from). For resource names,
deployment lanes, security boundaries, and operator access, use
[Hosting, Deployment, and Security](06-hosting-deployment-security.md). That
technical detail is not duplicated here.

## Ownership and billing rules

NCTQ ownership of Azure and Airtable is settled. The handoff should leave NCTQ
with administrator access, a backup administrator, invoice visibility, and a
documented renewal contact for each. A personal account should not be the only
way to administer a production service.

The Pydantic AI setup needs a named decision. Record who owns the Gateway and
Logfire organizations, who receives invoices, whether the Anthropic provider
relationship is included in Gateway billing or separately funded, and who can
change spend limits. The supplied material confirms the technical route but
does not identify the commercial owner.

Code currently lives in the Starling Strategy GitHub organization, while
production builds run through NCTQ's Azure DevOps organization. Treat those as
two distinct custody points. Decide in writing whether GitHub remains the
canonical long-term home or transfers to NCTQ, then document repository admins,
backup access, and how deploy branches continue to reach Azure DevOps.

## April to June 2026 measured Azure baseline

Dillon's figures came from Azure Cost Management and average the three full
months from April 1 through June 30, 2026. They measure Azure consumption in the
Microsoft Azure Sponsorship subscription. Each application line includes its
small managed Container Apps environment. The figures exclude Anthropic and
Pydantic AI model usage and do not establish the current cash invoice after
sponsorship credits.

| Azure resource group or service | Included in the measurement | Average per month | Annualized at that rate |
| --- | --- | ---: | ---: |
| Compass Frontend, `NCTQ_AI_PA_FE` | Frontend application and managed Container Apps environment | $75.47 | $905.64 |
| Policy Advisor API, `NCTQ_AI_PA_API` | API application and managed Container Apps environment | $93.64 | $1,123.68 |
| NCTQ Dashboard, `NCTQ_AI_Piper` | Dashboard application and managed Container Apps environment | $98.23 | $1,178.76 |
| Shared PostgreSQL database, `NCTQ_PA` | Production database virtual machine used by the platform | $91.65 | $1,099.80 |
| Shared data platform and email, `NCTQ_AI_Data` | Databricks, Data Factory, and login or reporting email services | $109.51 | $1,314.12 |
| **All five measured Azure groups** | **Three applications plus both shared groups** | **$468.50** | **$5,622.00** |

The direct Azure run cost for the public Compass Frontend and its Policy Advisor
API was $169.11 per month during this window. That subtotal is useful for
service-level planning, but it is not the full cost of operating Compass. The
chat also depends on the shared database and data platform. The dashboard uses
the same shared services and supports review and operations, so any allocation
of the $299.39 monthly dashboard and shared-service remainder must be stated
rather than implied.

## Model and API usage costs

Model spend is a separate variable cost. Compass sends model requests through
the Pydantic AI Gateway to Anthropic. Planner calls use a higher-capability
model, while bounded adjudication and applicable quality checks use cheaper
models. Offline model comparisons and evaluation sweeps also consume Gateway
budget even though they are not end-user traffic.

No April to June 2026 Anthropic or Pydantic AI invoice total was provided with
the handoff sources. Do not estimate one from Azure charges or present model
pricing tables as an invoice. For a refresh, reconcile the Gateway or provider
invoice with Logfire request spans. Prefer the per-request
`usage.pydantic_ai_gateway.cost_estimate` field for observed usage, while noting
that provider invoices remain the billing authority. See
[How Compass uses different AI models](02-product-and-answer-flow.md#how-compass-uses-different-ai-models)
for the model roles and the quality gates required before a cheaper model
replaces the current one.

Keep API-like services in separate budget lines:

| Cost class | Examples | Budget treatment |
| --- | --- | --- |
| Production model usage | Planner, catalog adjudicator, and model-judged criteria | Variable Compass operating cost, reconciled monthly |
| Evaluation model usage | Scenario sweeps, A/B runs, and disagreement judging | Variable development and quality cost, tagged separately from production traffic |
| Observability | Pydantic Logfire plan and retention | Fixed or tiered service cost, plus any usage overage |
| External data or analytics APIs | Airtable, Google Analytics, Urban Institute, WordPress, and other source integrations | Record subscription or usage fees separately, even when currently free or covered by another NCTQ account |

## Cost drivers and levers

The main Azure drivers are always-on application and environment capacity,
PostgreSQL virtual-machine sizing and storage, Databricks compute, Data Factory
runs, outbound networking, Log Analytics ingestion and retention, and retained
container images. Traffic growth can increase some of these costs, but idle
minimum capacity and scheduled data work create a base cost even with little
chat use.

The main model drivers are request volume, input and output tokens, prompt and
conversation length, model choice, retries, the number of model-backed quality
criteria, and evaluation sweep size. More concurrent work can also hit Gateway
limits without reducing total spend.

Use these levers in this order:

1. Remove unused Azure resources and duplicate revisions only after confirming
   that they are not part of rollback or production recovery.
2. Right-size minimum replicas, database capacity, Databricks jobs, log
   retention, and schedules using measured utilization and service-level needs.
3. Reduce avoidable model calls, repeated context, retries, and unnecessary
   output before changing model quality.
4. Use cheaper models for narrow tasks only after the structured-output,
   accuracy, and regression gates in the model-selection reference pass.
5. Separate production traffic from evaluation sweeps and cap each budget so a
   large test run cannot consume the operating allowance unnoticed.

## Budget refresh cadence

Refresh the operating view monthly, within ten business days after month end.
The budget owner should export Azure Cost Management by resource group, obtain
the Gateway or provider invoice, collect Logfire usage by environment and
model, and record shared-service invoices or allocation rules. Compare actuals
with the prior month and the April to June 2026 baseline, then explain material
changes in volume, rates, capacity, or scope.

Review the forecast quarterly and whenever one of these events occurs: a model
change, a provider price change, an Azure sizing change, a new data pipeline, a
new paid integration, a sponsorship-credit change, or a material traffic
increase. Rebuild the annual forecast from the latest trailing three complete
months. Do not keep annualizing the April to June baseline after newer complete
data exists.

Each refresh should state:

- the exact measurement window and whether figures are metered, invoiced, or
  forecast;
- taxes, credits, discounts, and sponsorship treatment;
- what each line includes and excludes;
- production versus staging or evaluation usage;
- the owner, billing account, currency, and allocation method;
- open ownership decisions and the person or role responsible for resolving
  them.

The budget owner and approval threshold are not identified in the supplied
material. Assign both explicitly. Until then, mark them as unassigned rather
than naming an assumed account holder.

## Credential locations

This inventory names locations and purposes only. It intentionally contains no
values, tokens, passwords, personal data, or resolved secret references.

| Location | Credential names or categories | Purpose |
| --- | --- | --- |
| Azure Container App secrets | PostgreSQL connection fields, frontend-to-API token, Pydantic AI Gateway key, Logfire token, dashboard analytics credentials, and email settings | Runtime configuration for the three production applications |
| Azure Key Vault `nctq-ai-data-kv`, linked through Databricks scope `nctq-secrets` | SQL Server reader, PostgreSQL writer, Airtable PAT, WordPress application password, Azure Communication Services settings, and Databricks deployment PAT | Nightly data ingestion, production loads, publication sync, reporting, and notebook deployment |
| Starling Strategy 1Password vaults approved for shared operations | NCTQ database logins, Airtable sync credential, Gateway and model-provider keys, Logfire credentials, Azure or deployment administration credentials, and staging service tokens | Human and agent operations outside Azure-managed runtime secret stores |
| Repository `.env.op` | Secret references only, never resolved values | Starts local evaluation and operational commands through 1Password at runtime |
| Project `.mcp.json` | Launch configuration that resolves referenced staging credentials at process start | Read-only operational tooling without storing a database password in the repository |
| Azure DevOps service connections | Container registry and deployment authorization | Pipeline image build and delivery |

Before transfer, verify each live credential has one authoritative secret store,
a descriptive item name, a purpose and scope note, a current owner, and a
rotation procedure. Do not copy a value into this document, a ticket, source
control, or a shell profile.

## Scope of this budget

This section covers the current Compass service and the platform resources it
actually depends on: the public frontend, Policy Advisor API, shared production
database, active data refresh path, operational dashboard where allocated, model
calls, observability, and required integrations.

It does not automatically include unrelated legacy systems, archived schemas or
applications, retired AI pipelines, separate NCTQ web properties, other
Starling Strategy clients, or parallel experiments that do not serve or test
the active Compass runtime. Shared Azure or software subscriptions belong here
only to the extent that Compass uses them, with an explicit allocation method.
When a resource supports both Compass and another project, report the full
invoice separately from the Compass allocation so neither is mistaken for the
other.

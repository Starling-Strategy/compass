# 7. Accounts and Service Ownership

This page is an operational handoff checklist for the external accounts and
services that Compass depends on. It records who should own or administer each
account and what setup or transfer remains.

## Ownership checklist

| Account or service | Owner or administrator | Status | Remaining action |
| --- | --- | --- | --- |
| Microsoft Azure Sponsorship subscription | NCTQ | In place | Confirm the primary and backup NCTQ administrators during handoff. |
| NCTQ Airtable | NCTQ | In place | Confirm the operational base owner and backup owner. |
| Pydantic AI Gateway | NCTQ | To be set up by NCTQ | NCTQ should create the organization account and retain primary and backup administrator access. |
| Pydantic Logfire | NCTQ | To be set up by NCTQ | NCTQ should create the workspace and retain primary and backup administrator access. |
| GitHub repository `Starling-Strategy/compass` | NCTQ custody | To be set up by NCTQ | Establish NCTQ organization ownership or administrator custody. Starling can retain support access as agreed. |
| Azure DevOps organization `nctqai` | NCTQ, with Starling support | In place | Confirm the named NCTQ administrators and pipeline service-connection owners. |
| Google Analytics | NCTQ | In place | No transfer action is required; NCTQ controls the property. |

## Handoff rules

- Use organization-owned email accounts for primary ownership and backup administration. A personal account should not be the only way to administer a production service.
- Record final owner and administrator names in the internal access register.
- Keep passwords, API keys, tokens, and other secret values in the approved secret manager, not in this repository or documentation.
- Treat Starling support access as delegated access; NCTQ ownership should not depend on Starling being the sole administrator.

For deployment authorities, environment boundaries, and operator access, see
[Hosting, Deployment, and Security](06-hosting-deployment-security.md). For
dashboard roles and staff access, see [Administration and Dashboard](05-administration-and-dashboard.md).

## 7.1 Costs and budget

This section is the financial-planning companion to the ownership checklist
above. Its figures are a dated operating baseline, not a current invoice. Keep
Azure hosting, model usage, and shared services separate so a change in one does
not hide a change in another.

### April to June 2026 measured Azure baseline

These figures came from Azure Cost Management and average the three full months
from April 1 through June 30, 2026. They measure consumption in the Microsoft
Azure Sponsorship subscription. Each application line includes its small managed
Container Apps environment. The figures exclude Anthropic and Pydantic AI model
usage and do not establish the current cash invoice after sponsorship credits.

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

### Model and API usage costs

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

### Cost drivers and levers

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

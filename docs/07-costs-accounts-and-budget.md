# 7. Costs, Accounts, and Budget

This page is an operational handoff checklist for the external accounts and
services that Compass depends on. It records who should own or administer each
account, **who pays for it**, and what setup or transfer remains.

## Ownership and payer inventory

This is the full inventory of accounts and subscriptions the Compass platform
touches. It is organized by what the service does, and every line names both an
administrator and a payer, because those are separate questions.

**The billing intent, stated plainly:** no Compass or NCTQ.ai service should
run on a Starling-owned billing instrument. The target end state is that every
paid line below is billed directly to NCTQ, so there is nothing for Starling to
front and pass through as an invoice. Where the payer column says *confirm*,
that reconciliation has not yet been verified against the provider's billing
record — those are the lines to close during handoff, and they are the reason
this table exists.

### Cloud and hosting

| Account or service | Administrator | Who pays | Status | Remaining action |
| --- | --- | --- | --- | --- |
| Microsoft Azure Sponsorship subscription | NCTQ | NCTQ (sponsorship credits) | In place | Confirm the primary and backup NCTQ administrators. Confirm the sponsorship expiry date and what the run rate becomes at commercial pricing. |
| Azure DevOps organization `nctqai` | NCTQ, with Starling support | Included in the Azure subscription | In place | Confirm the named NCTQ administrators and the pipeline service-connection owners. |
| Cloudflare DNS for `nctq.ai` (covers `staging.nctq.ai` and `umami.nctq.ai`) | NCTQ | NCTQ | In place | Confirm the Cloudflare account owner and backup administrator, and who is authorized to change DNS records. |
| TLS certificates for the three applications | NCTQ | No separate charge (Azure-managed certificates) | In place | None; renewal is managed by Azure Container Apps. |

### Source code and repositories

| Account or service | Administrator | Who pays | Status | Remaining action |
| --- | --- | --- | --- | --- |
| GitHub repository `Starling-Strategy/compass` | NCTQ custody | **Confirm** (GitHub plan) | To be set up by NCTQ | Establish NCTQ organization ownership or administrator custody. Starling can retain support access as agreed. |

### AI models and observability

| Account or service | Administrator | Who pays | Status | Remaining action |
| --- | --- | --- | --- | --- |
| Pydantic AI Gateway (production model traffic) | NCTQ | NCTQ, billed through Logfire | Configured | Confirm NCTQ billing and admin access; reconcile the application estimate against the Gateway ledger monthly. |
| Google AI / Gemini API | — | — | **Not a Compass account** | None for Compass. Gemini is used by the [Metric Calculator](reference/metric-calculator.md) and its document pipeline, which are upstream data-production tools rather than part of Compass. Noted here only so the platform's one non-Anthropic model dependency is not mistaken for a Compass service; it belongs to the Metric Calculator's own ownership record. |
| Pydantic Logfire | NCTQ | NCTQ | Connected; `nctqai` project queried | Confirm the named NCTQ project administrators and the retention plan. |

### Data sources and integrations

| Account or service | Administrator | Who pays | Status | Remaining action |
| --- | --- | --- | --- | --- |
| NCTQ Airtable (publications catalog) | NCTQ | NCTQ | In place | Confirm the operational base owner and backup owner. |
| NCTQ TCD / Pathfinder database | NCTQ | NCTQ | In place | Confirm the read credential used by the nightly sync and its rotation owner. |
| NCTQ WordPress (Pathfinder site) | NCTQ | NCTQ | In place | Confirm the API/read credential used by the sync, if any remains active. |
| Urban Institute Education Data API (NCES context) | — | Free public API | In place | Confirm whether the current usage requires a registered key or stays anonymous. |
| Azure Databricks and Data Factory | NCTQ | NCTQ (inside `NCTQ_AI_Data`) | In place | Confirm workspace administrators and the owner of each notebook schedule (see the [notebook inventory](reference/databricks-notebook-inventory.md)). |

### Analytics and email

| Account or service | Administrator | Who pays | Status | Remaining action |
| --- | --- | --- | --- | --- |
| Google Analytics | NCTQ | NCTQ | In place | No transfer action required; NCTQ controls the property. It is the intended single analytics source; retiring the second tool is tracked in [§9](09-known-issues-and-limitations.md#two-analytics-tools-measure-the-same-thing-google-analytics-should-be-the-only-one). |
| Transactional email for dashboard login codes | NCTQ | NCTQ (inside `NCTQ_AI_Data`) | In place | Confirm the sending domain, the `noreply@nctq.ai` sender configuration, and the credential owner. |

### Credential register

The inventory above covers *accounts*. Credentials are tracked separately,
because one account can hold several and each needs a named rotation owner.
This documentation deliberately holds **no secret values** — the register
itself belongs in the approved secret manager, not in this repository.

What the register must cover, one row per credential: the credential's purpose,
which application and environment consumes it, the environment-variable name it
arrives under, where the value is stored, who may read it, who may rotate it,
and the last rotation date.

The credential families in use, as a checklist for building that register:

| Credential family | Consumed by | Environment names |
| --- | --- | --- |
| PostgreSQL connection credentials (per environment) | API, dashboard, data sync | `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_USER`, `PG_PASSWORD`, `PG_SCHEMA` |
| Model routing key | API | `PYDANTIC_AI_GATEWAY_API_KEY` |
| Telemetry tokens (write and read are separate) | API, dashboard | `LOGFIRE_TOKEN`, `LOGFIRE_READ_TOKEN` |
| Compass API keys, minted per consumer | Frontend, dashboard server-side calls, scripts, eval harness | `FASTAPI_API_TOKEN`, `FASTAPI_ADMIN_API_TOKEN`; stored as hashes in `compass.api_keys` |
| Dashboard session and cookie signing material | Dashboard | See [`dashboard/src/nctqai/config.py`](../dashboard/src/nctqai/config.py) |
| Email delivery credential for login codes | Dashboard | SMTP settings in the dashboard config |
| Analytics credentials | Dashboard | Google Analytics service-account material. The Umami instance's credentials are still live until that tool is retired ([§9](09-known-issues-and-limitations.md#two-analytics-tools-measure-the-same-thing-google-analytics-should-be-the-only-one)); revoke them as part of decommissioning |
| Azure and Azure DevOps access | Operators, release pipelines | Azure RBAC and pipeline service connections, not environment variables |
| Databricks workspace and source-system credentials | Databricks notebooks | Databricks secret scopes |
| Cloudflare DNS access | Operators | Cloudflare account roles, not environment variables |

One credential is deliberately absent from that list: the Gemini API key used by
the Metric Calculator's document pipeline. It is a data-production credential,
not a Compass one, and belongs in that project's register.

The rotation procedure for Compass API keys — the credential family that
changes most often — is in [§5](05-administration-and-dashboard.md#safe-rotation-and-revocation).
Treat any credential change as a release, per [§6.5](06-hosting-deployment-security.md#65-runtime-configuration-and-secrets).

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

#### What this estimate does and does not include

Read the $468.50 figure with these boundaries, because the most common way to
misread a cloud bill is to compare two numbers that cover different things:

**In scope:** the five resource groups above — the three application groups
(each including its managed Container Apps environment), the shared PostgreSQL
virtual machine, and the shared data platform and email group.

**Out of scope, and deliberately so:**

- **Model and API usage.** Gateway/model spend is a separate variable cost,
  covered in the next section. It is not in the Azure figure at all.
- **Other Azure resources in the same tenant that predate or sit outside the
  Compass platform.** The Azure Sponsorship subscription may carry legacy or
  unrelated NCTQ resources; those costs are not Compass costs and should not be
  attributed to this platform. When reconciling against an Azure invoice, filter
  by the five named resource groups rather than reading the subscription total.
  Anything outside those groups belongs to a different budget line and needs its
  own owner.
- **Sponsorship credits.** These are consumption figures at listed rates, not
  cash charged after credits are applied. They are the right number for
  forecasting what the platform will cost when credits end; they are the wrong
  number for reporting what NCTQ paid last month.
- **Staging.** Staging runs outside Azure and is not in these figures. It is a
  development environment, not part of the production service path
  ([§6](06-hosting-deployment-security.md#62-applications-and-environments)).
- **Services billed elsewhere.** Logfire, Airtable, Google Analytics,
  Cloudflare, and GitHub are separate lines, each in the ownership inventory
  above.

Refresh cadence: re-measure the Azure baseline and the Gateway ledger together
at the monthly operating review, and re-derive the annualized figures whenever
the measured months change by more than a rounding error, sponsorship status
changes, or a resource group is added or retired.

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

#### Usage records and cost estimates

Compass records usage for captured model calls associated with chat sessions
and messages in `compass.llm_usage`. A usage row can include the model, phase,
input tokens, output tokens, cache-read tokens, cache-write tokens, request
count, source (`chat` or `eval`), and pricing status. This gives us a durable
way to estimate production and evaluation usage from the conversations already
stored by the application.

The estimate should sum the recorded token categories and apply the model price
that was in effect for each call. The application path uses `genai-prices` and
preserves rows that cannot be priced, rather than silently treating them as
free. Group estimates by date, environment, model, and source so production
conversations are not mixed with staging, evaluation, or A/B traffic. Keep
cache-read and cache-write tokens separate from fresh input and output because
they have different pricing treatment.

This is an application-level estimate, not a Gateway invoice. It can be
incomplete when a call has no usage record, a model is absent from the pricing
catalog, prices change, or the Gateway applies provider-specific pricing or
markup. Missing telemetry must be labeled unknown rather than reported as
zero.

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

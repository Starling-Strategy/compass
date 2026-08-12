# 6. Hosting, Deployment, and Security

Compass runs as three applications that share data but have separate operational
roles. Production runs on Microsoft Azure. Staging runs on Coolify and is a
separate deployment path. Local development runs the same application code on a
developer machine against approved configuration. This private handoff explains
how to release, verify, secure, observe, and recover the system without including
credentials, private hostnames, or personal data.

## 6.1 Authority and scope

Use this order when sources disagree:

1. Current application code and settings models.
2. Repository `AGENTS.md` files and active operator skills.
3. Current operational notes in this repository.
4. The attached handoff documents, which describe the Azure estate and its
   historical deployment lanes.

This section documents the operating model. It does not replace the application
specific runbooks, database migration procedure, or incident records. Commands
use placeholders and must be filled from the approved password manager and
cloud inventory at execution time.

Human access and Dashboard administration are covered in [Administration and
Dashboard](05-administration-and-dashboard.md). Account ownership, service
handoff, and spending are covered in [Accounts and Service Ownership](07-costs-accounts-and-budget.md).

## 6.2 Applications and environments

The platform has three deployable applications:

| Application | Active source | Role | Local default | Production host |
| --- | --- | --- | --- | --- |
| Policy Advisor API | `backend/src/compass_backend/` | FastAPI chat engine, deterministic data execution, session persistence, and post-response quality verdicts | Port `8000` | Azure Container Apps |
| Compass Frontend | `frontend/` | PHP and Apache chat interface and server-side proxy to the API | Port `3000`; container port `80` | Azure Container Apps |
| NCTQ Dashboard | `dashboard/src/nctqai/` | FastHTML staff dashboard, Metric Calculator, and Compass observability surfaces | Port `5001` | Azure Container Apps |

### Deployment matrix

| Environment | Purpose | Source or branch posture | Runtime | Database posture | Release method |
| --- | --- | --- | --- | --- | --- |
| Local | Development, focused tests, and browser replay | Feature branch based on current `main` | Local processes or containers | Use staging only when approved and reachable. Use `PG_*` settings and `PG_SCHEMA=compass` | Start each service locally. No cloud release |
| Staging | Integration validation, deploy-shape checks, and reviewer links | Coolify builds the repository `staging` branch | Coolify on a private tailnet host | Staging PostgreSQL. Agent and MCP inspection is read-only | Apply pending staging migrations first, move reviewed code to `staging`, then manually trigger only the affected Coolify applications |
| Production | Public and staff service | Reviewed production release lane sourced from approved GitHub code | Azure Container Apps in Central US | Shared production PostgreSQL in the `compass` schema. Human and agent inspection is read-only | Mirror the approved production source to Azure DevOps, queue the application pipeline, publish to ACR, and verify the new Container Apps revision |

Staging is not a smaller Azure production deployment. It is Coolify-only, does
not rebuild automatically on a push, and can be reached for deployment only from
the private network. Keep detailed Coolify identifiers, tokens, and host details
in the staging deployment skill and approved credential store, not in this
handoff.

## 6.3 Production Azure estate

The handoff inventory places production in the Microsoft Azure Sponsorship
subscription in Central US. Confirm subscription and region in Azure before any
change. The major resource groups are:

| Resource group | Major services | Operational purpose |
| --- | --- | --- |
| `NCTQ_AI_PA_API` | Policy Advisor API Container App, Container Apps environment, Azure Container Registry, virtual network, NAT gateway, Log Analytics | Runs and observes the chat API |
| `NCTQ_AI_PA_FE` | Compass Frontend Container App, Container Apps environment, Azure Container Registry, managed TLS certificates, virtual network, NAT gateway, Log Analytics | Serves the public chat interface and proxies API calls |
| `NCTQ_AI_Piper` | NCTQ Dashboard Container App, Container Apps environment, Azure Container Registry, managed TLS certificate, virtual network, NAT gateway, Log Analytics | Serves authenticated staff tools and operational views |
| `NCTQ_PA` | Production PostgreSQL virtual machine | Hosts the shared production database |
| `NCTQ_AI_Data` | Azure Databricks, Azure Data Factory, and the email service used for login codes | Prepares source data and supports dashboard authentication |

Each application has an isolated Container App, managed environment, registry,
networking, and logging lane. The database and data preparation services are
shared dependencies.

### Source provenance that needs confirmation

The intended clean `compass` repository contains all three active
codebases listed above. Dillon's Azure handoff describes three separate GitHub
deploy repositories, three production deploy branches, and matching Azure
DevOps repositories. These statements can both be true if the separate
repositories are deployment mirrors or legacy packaging lanes, but the supplied
material does not prove that relationship.

Before the next production release, the platform owner must confirm:

- which GitHub commit is the canonical source for each production image;
- whether the per-application repositories remain active mirrors or are legacy;
- which production deploy branch and Azure DevOps repository each pipeline
  currently watches;
- whether production settings are still carried as deploy-branch differences or
  now live entirely in Container Apps configuration.

Do not consolidate or retire a production lane based only on this draft.

## 6.4 Production release flow

The expected production path is separate for each application:

```text
approved GitHub source
  -> approved production deployment lane
  -> Azure DevOps repository and pipeline
  -> application-specific Azure Container Registry
  -> new Azure Container Apps revision
  -> revision, log, health, and user-flow verification
```

Azure DevOps builds an immutable container image and tags it with a traceable
build identifier. The pipeline pushes that image to the application's Azure
Container Registry. Azure Container Apps creates a revision from the image and
runtime configuration. A successful image build does not prove that the new
revision started or received traffic.

Representative command shapes:

```bash
# Queue the confirmed application pipeline.
az pipelines run \
  --org <azure-devops-organization-url> \
  --project <project> \
  --id <pipeline-id> \
  --branch <mirrored-branch>

# Inspect revisions, then inspect the selected revision's startup logs.
az containerapp revision list \
  --name <app> \
  --resource-group <resource-group> \
  --output table

az containerapp logs show \
  --name <app> \
  --resource-group <resource-group> \
  --revision <revision> \
  --tail 40
```

Do not copy a short-lived access token into a command history, document, ticket,
or log. Obtain deployment authentication through the approved Azure login and
credential process at execution time.

### Release checklist

- [ ] Confirm the issue, reviewed change, release owner, and exact Git SHA.
- [ ] Confirm the worktree contains current `origin/main` and no unreviewed
      local changes are entering the release.
- [ ] Run the smallest complete local validation for every changed boundary.
      Use `./scripts/check.sh` for the full repository check when appropriate.
- [ ] For user-visible Compass behavior, run the required B-spine case replay
      and scorecard validation.
- [ ] For frontend, SSE, citation, export, or dashboard interaction changes,
      replay the affected flow in a browser.
- [ ] Validate locally against staging, then deploy and verify staging when a
      deploy-shape check or external review URL is required.
- [ ] Identify and apply required schema migrations before application code.
      Never deploy code that expects a database object which is not present.
- [ ] Confirm the production source mapping, Azure subscription, resource group,
      pipeline, registry, Container App, image tag, and current good revision.
- [ ] Queue one application lane at a time unless the release plan explicitly
      coordinates several applications.
- [ ] Confirm the Azure DevOps build succeeded and the expected image exists in
      the correct registry.
- [ ] Confirm the newest Container Apps revision is running and has active
      traffic. Do not infer this from a green build.
- [ ] Read startup logs for missing settings, dependency errors, and schema
      warnings.
- [ ] Run the application's health check and a focused user-flow smoke test.
- [ ] Record the Git SHA, pipeline build, image tag, revision, verification
      result, operator, and time in the approved release record.
- [ ] Keep the previous known-good revision available until verification and the
      observation window complete.

## 6.5 Runtime configuration and secrets

Supply environment-specific configuration at runtime. Do not bake credentials
into images, repository files, JavaScript, page source, build output, or release
notes. Store sensitive production values as Azure Container Apps secrets and
reference them from environment variables. Limit secret read and write access to
the smallest operator and service set that needs it.

The API reads PostgreSQL configuration from `PG_HOST`, `PG_PORT`,
`PG_DATABASE`, `PG_USER`, `PG_PASSWORD`, and `PG_SCHEMA`. The canonical schema
is `compass`. Its model traffic uses `PYDANTIC_AI_GATEWAY_API_KEY`; do not add
direct provider keys that bypass the shared gateway. Logfire uses separate
telemetry credentials. Secret settings are typed as secrets in application code
and must be unwrapped only at the client boundary.

The Frontend keeps `FASTAPI_API_TOKEN` and its API endpoint server-side. PHP
proxy routes add the token to backend requests. Browser code must never receive
it.

The Dashboard has its own database, session, SMTP or email, analytics, and
Logfire settings. Keep login delivery credentials, analytics service-account
material, cookie signing material, and database passwords in the runtime secret
store. Compass observability routes read `compass.*`; the Metric Calculator is a
separate workflow that can write validated metric data.

Treat a configuration or secret change as a release. Record the change, create
or restart the intended revision using the approved Azure method, and repeat
startup, health, and smoke verification. Never print current secret values while
comparing configuration.

## 6.6 Security boundaries

| Boundary | Control | Operator rule |
| --- | --- | --- |
| Public browser to Frontend | HTTPS with an Azure managed TLS certificate | Keep HTTPS active, renew and bind certificates through Azure, and do not expose backend credentials to the browser |
| Frontend to API | Server-side bearer token over HTTPS | Store the token only in Frontend runtime secrets and rotate it with coordinated verification |
| Direct API access | `Authorization: Bearer pa_<env>_<token>`; SHA-256 hash lookup in `compass.api_keys` | Prefixes identify environment but are not the security control. Soft-revoke keys with `revoked_at`; never hard-delete audit records |
| API administration | Key owner resolves through `compass.api_keys.owner_email` to `compass.users.is_admin` on every request | Role changes take effect on the next request. Keep production auth enabled |
| Dashboard user access | Email one-time code, session cookie, and central role map with `viewer`, `analyst`, `power_user`, and `admin` | Manage access through the central user and role model. Do not add route-specific bypasses or a second role list |
| Application to PostgreSQL | Runtime database credential and schema setting | Use separate environment credentials, least privilege, encrypted transport where configured, and `compass` as the canonical schema |
| Operator or agent to staging PostgreSQL | Read-only MCP or an explicitly approved migration procedure | Treat any non-`SELECT` through the staging read surface as a guardrail violation |
| Operator or agent to production PostgreSQL | Read-only inspection only | Never make the session writable and never execute a mutating statement |
| Data platform to database | Scheduled, controlled load path | Use the documented sync and migration procedures. Do not improvise production writes |
| Model and telemetry services | Pydantic AI Gateway and Logfire credentials held by the API | Route models through the gateway. Keep telemetry tokens separate from model credentials |
| Cloud control plane | Azure role-based access, short-lived login, and application-specific pipeline service connections | Use least privilege, avoid shared long-lived credentials, and preserve an auditable release trail |

The production-write guardrail applies to human and agent operational access.
It does not mean the runtime applications are stateless: the API persists
conversations and verdicts, and the Metric Calculator has a controlled write
workflow. Those writes must occur through application code and scoped runtime
identities, not ad hoc operator SQL.

The attached platform overview says all three applications read and write the
shared database. Current repository guidance is narrower: the Dashboard's
Compass observability surface reads `compass.*`, while its Metric Calculator
writes validated metric data. Confirm the deployed database roles and grants,
then update the infrastructure inventory if they do not match this boundary.

## 6.7 Logging and observability

All three production application lanes send container and platform logs to
Azure Log Analytics. Use those logs for revision startup, crashes, ingress,
resource pressure, and platform events. The API and Dashboard also use Pydantic
Logfire for application traces when configured.

A fresh backend chat turn should have one `compass.turn` root span covering
session load through persistence. When investigating a report, bind together
the session, assistant message, turn snapshot, SSE completion payload, trace ID,
and Logfire trace. Do not diagnose only from the rendered response. Telemetry
failure must remain observable, but non-blocking telemetry should not prevent an
otherwise healthy chat response.

Post-response quality evaluation writes live and sweep verdicts to
`compass.verdicts`. The Quality Scorecard reads this ledger. Nightly sweeps are
a staging Coolify scheduled task, not a production Azure release step. Keep
live user-turn verdicts separate from sweep retention and never prune them by a
blanket creation-date rule.

Logs and traces can contain operational context. Apply the organization's
retention and access controls, avoid adding secrets or full credentials to log
attributes, and restrict exported diagnostics to the incident team.

## 6.8 Health checks and release evidence

Use these checks as signals with different meanings:

| Application | Check | What it proves |
| --- | --- | --- |
| Policy Advisor API | `GET /api/v1/health` | Process liveness and reported database status. The route is public and returns liveness while the process is serving |
| Policy Advisor API | `GET /api/v1/ready` | Readiness for traffic, including dependency state used by the application |
| Compass Frontend | HTTPS request to `/` | PHP and web ingress respond. Follow with a proxied chat or affected-flow smoke test |
| NCTQ Dashboard | `GET /health` | Dashboard process responds. Follow with authenticated access to the affected staff surface |
| Azure Container App | Revision state, traffic assignment, startup logs, and image identifier | The intended revision, not merely an older healthy revision, is serving |

A release is complete only when the pipeline succeeded, the expected image is
present, the intended revision is running and receiving traffic, startup logs
are clean, the health endpoint passes, and the affected user flow works. Health
alone can pass while an older revision still serves traffic.

## 6.9 Rollback procedure

Azure Container Apps revisions are the primary application rollback unit.
Rollback restores service first, then preserves evidence for diagnosis.

1. Declare the release unhealthy and pause further releases for the affected
   application.
2. Record the failing revision, image tag, build, Git SHA, configuration change,
   first observed symptom, and current traffic assignment.
3. Check whether the incident is application code, runtime configuration,
   database schema, data, network, model gateway, or another dependency.
4. If the previous revision is compatible with the current schema and data,
   reactivate it and move all traffic back to that known-good revision.
5. Verify revision state, startup logs, the application health endpoint, and the
   affected user flow.
6. Preserve the failed revision and logs until evidence is captured. Do not
   delete it during the initial response.
7. If traffic rollback is unavailable, deploy the exact last-known-good image as
   a new revision and repeat verification.
8. Record recovery time, customer impact, and the final serving revision. Open
   follow-up work for the root cause and any missing alert or runbook step.

Representative traffic command shape, after the exact revision names have been
verified:

```bash
az containerapp ingress traffic set \
  --name <app> \
  --resource-group <resource-group> \
  --revision-weight <known-good-revision>=100 <bad-revision>=0
```

Do not attempt to undo an append-only database migration as an automatic part
of application rollback. First determine whether the prior application revision
is forward-compatible with the migrated schema. If a database correction is
required, use an approved forward migration with an explicit backup, review,
and production change plan.

## 6.10 Incident response

1. **Triage.** Identify the affected environment and application. Check ingress,
   revision state, health, recent releases, Log Analytics, Logfire, database
   reachability, and gateway or provider status.
2. **Contain.** Stop the release lane. Roll traffic back when a recent revision
   is implicated. Revoke or rotate a credential when exposure is suspected.
3. **Protect data.** Keep production operator sessions read-only. Do not repair
   data with ad hoc SQL. Preserve logs, traces, revision metadata, and audit
   records without copying personal data into tickets.
4. **Communicate.** Name the affected surface, user impact, start time, current
   mitigation, and next update owner. Do not publish credentials, private
   network details, or unverified root-cause claims.
5. **Recover and verify.** Restore the known-good revision or dependency, then
   run the full health and user-flow checks for that application.
6. **Learn.** Write a timeline and root cause, add a regression case when the
   failure was user-visible, strengthen monitoring or the runbook, and link the
   corrective work to the appropriate issue and review process.

Escalate immediately when an incident may involve credential exposure,
unauthorized access, loss or corruption of production data, TLS failure, or
material unavailability. Credential rotation, database changes, and public
communications require the designated platform owner.

## 6.11 Open operational questions

- Is the clean `compass` repository now the sole source of all three
  production applications, or do the separate GitHub and Azure DevOps mirrors
  in the handoff remain active deployment authorities?
- What are the currently approved production deploy branches and pipeline
  triggers for each application?
- Does Azure Container Apps still use single active revision mode for all three
  applications, and what is the retention policy for inactive revisions?
- Which database roles and grants are assigned to each production application,
  especially the Dashboard's read-only Compass surface and write-capable Metric
  Calculator?
- Are database connections configured to require TLS in both staging and
  production? The supplied sources establish HTTPS ingress but do not document
  the current PostgreSQL TLS enforcement setting.
- Which Azure monitor alerts, notification routes, recovery objectives, and
  incident owner are currently approved? The supplied sources describe logging
  but do not establish a complete alert policy.
- The root repository guidance says there is no CI gate, while
  `docs/ops/sweep-cadence.md` describes a GitHub Actions test and lint workflow.
  Confirm the current repository automation before relying on either statement
  as a release gate. In all cases, local validation remains mandatory.

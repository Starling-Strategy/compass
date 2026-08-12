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
| Google Analytics | NCTQ | Transfer pending | Transfer the property from Dillon to NCTQ and confirm the NCTQ property owner, administrator, and service-account custodian. |

## Handoff rules

- Use organization-owned email accounts for primary ownership and backup administration. A personal account should not be the only way to administer a production service.
- Record final owner and administrator names in the internal access register.
- Keep passwords, API keys, tokens, and other secret values in the approved secret manager, not in this repository or documentation.
- Treat Starling support access as delegated access; NCTQ ownership should not depend on Starling being the sole administrator.

For deployment authorities, environment boundaries, and operator access, see
[Hosting, Deployment, and Security](06-hosting-deployment-security.md). For
dashboard roles and staff access, see [Administration and Dashboard](05-administration-and-dashboard.md).

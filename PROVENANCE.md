# Provenance

This repository's application code is a curated snapshot of the code running in
production on Azure Container Apps. This file records exactly where each tree
came from, what was included, and every place the copy deviates from the
source — so "this is the production code" is auditable, not just asserted.

## Source commits

Production deploys from a dedicated deploy branch in each application's source
repository. The snapshots below are those deploy branches at the commits
current as of 2026-08-12:

The source repositories below are **private**; the links are recorded for
internal traceability and will not resolve for public readers.

| App | Directory | Source repository | Deploy branch | Commit | Committed |
| --- | --- | --- | --- | --- | --- |
| Policy Advisor API | `backend/` | Starling-Strategy/policy-advisor | `nctq-azure-push` | `7d44341644d88a7fe6434c1567a9c3c0c3ef2327` | 2026-07-01 |
| Compass Frontend | `frontend/` | Starling-Strategy/nctqai-compass-frontend | `nctq-azure-push` | `41424e4629231d245d164f9fb6286b34ad58fc9a` | 2026-07-06 |
| NCTQ Dashboard | `dashboard/` | Starling-Strategy/nctqai-dashboard | `azure-nctq-push` | `80f8c11ff024f6ad92421682e056b40d70c5a760` | 2026-07-15 |

These commits are the deploy-branch tips, which are treated as the production
state. The definitive check — reading each Container App's running image tag
(an Azure DevOps build number) and mapping it back to the commit that build
compiled — requires Azure access and can be re-run at any time to reconfirm.

## What was included

The rule: each directory contains what that application's Docker image is
built from, plus the Dockerfile itself — nothing more.

- **`backend/`** — the files `Dockerfile.api` copies into the image:
  `src/compass_backend/`, `content/`, `scripts/entrypoint.sh`,
  `pyproject.toml`, `uv.lock` (byte-identical, since they pin dependency
  resolution), plus a replacement `README.md` (see deviations). The image also
  carried `static/`, which has since been removed (deviation 5).
- **`frontend/`** — the image is built from the whole repository minus its
  `.dockerignore` exclusions; this copy applies the same rule (so `tests/`,
  `infra/`, and CI config are out, exactly as they are absent from the image).
- **`dashboard/`** — `src/` in full (including its vendored slice of
  `compass_backend` shared code, which production genuinely runs),
  `pyproject.toml`, and `requirements-dashboard.txt`.

## Deviations from the source

Every difference between this copy and the source commits, in full:

1. **`backend/Dockerfile`** is the source repo's `Dockerfile.api`, renamed
   (the source repo carries several Dockerfiles; only this one builds the
   production API image).
2. **`backend/README.md`** is a clean replacement. The source repo's root
   README describes the internal development environment (staging URLs,
   private-network setup) and is copied into the image only because the
   Dockerfile references it; its content has no runtime effect.
3. **Private database host IPs redacted** to `<private-db-host>` in
   non-runtime documentation files only: `backend/src/compass_backend/README.md`
   and `dashboard/src/document_pipeline/` (design docs and one SQL comment).
   No runtime code was modified.
4. **CI/deployment config omitted** from all three apps
   (`azure-pipelines.yml`, `infra/`): these files build no part of the
   production images and are not vendored here. The operational deployment
   model they support — release flow, environments, and security
   boundaries — is documented in
   [docs/06-hosting-deployment-security.md](docs/06-hosting-deployment-security.md),
   without embedding credentials or live pipeline identifiers.

5. **`backend/static/` removed.** The source repo's `static/index.html` was a
   legacy Azure AD admin console that no route served — nothing under
   `src/compass_backend/` mounts a static directory — but the Dockerfile copied
   it into the image. It embedded real Entra tenant, client, and API-scope
   identifiers in client-side JavaScript. The file and its `COPY` line are both
   gone. No runtime behavior changed, because nothing served it.

6. **Internal engineering docs removed** from the dashboard copy
   (`src/document_pipeline/docs/`, 12 files). Design and planning artifacts
   swept in by the "vendor `src/` in full" rule; no code imports or links to
   them. They contained a production database endpoint, an internal analytics
   host, and quotes from a client call.

7. **Identities and internal links redacted** for public release: a seeded
   administrator's personal email in `001_auth_tables.sql` (now a commented
   template), a contractor's address in six test modules, a client
   stakeholder's first name across the prediction pipeline (renamed to "the
   silence rule"), a staff name in the Databricks inventory, four internal
   Google Doc URLs, and a Vespa tenant name. Test and evaluation behavior is
   unchanged; the one renamed identifier, `silence_rule_violations`, is written
   but never read.

Everything else is byte-identical to the source commits — including code
comments that reference the development environment and test fixtures that use
staging hostnames, since altering them would break the fidelity guarantee.

## Not vendored

The Databricks data-platform notebooks (bronze → silver → gold pipeline) live
in their own workspace and are documented in
[docs/03-data-and-databricks.md](docs/03-data-and-databricks.md) rather than
copied here.

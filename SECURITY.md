# Security Policy

## Reporting a vulnerability

Please report security issues privately. Do **not** open a public issue.

- Preferred: [open a private security advisory](https://github.com/Starling-Strategy/compass/security/advisories/new)
  on this repository. Private vulnerability reporting is enabled.
- Alternative: email `policypathfinder@nctq.org` with "SECURITY" in the subject.

Please include what you found, how to reproduce it, and what an attacker could
do with it. If you have a suggested fix, we welcome it, but a clear report is
enough.

We aim to acknowledge reports within five business days.

## Scope

This repository holds a curated snapshot of the code running Compass, the AI
research assistant behind NCTQ's District Policy Pathfinder. In scope:

- The Policy Advisor API (`backend/`)
- The public chat frontend (`frontend/`)
- The internal review dashboard (`dashboard/`)

Please do **not** test against live NCTQ systems. Report what you find in the
code and let us reproduce it ourselves. Automated scanning of production hosts
is not authorized.

## What this snapshot is

The code here is published for transparency and handoff, and mirrors production
at the commits recorded in [PROVENANCE.md](PROVENANCE.md). It is not a
maintained, independently deployable distribution, and it carries no
deployment credentials, connection strings, or secret values — configuration
arrives through environment variables at runtime.

Because the snapshot tracks production for fidelity, some code comments and
test fixtures reference internal development environments. Those references are
deliberate and are not themselves access paths.

## Supported versions

Only the current `main` branch is tracked. Older commits and merged branches
are historical and receive no fixes.

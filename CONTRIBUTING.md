# Contributing

Thanks for your interest in Compass.

## What this repository is

Compass is published for transparency and for NCTQ's operational handoff. It is
a **curated snapshot of production code**, not an independently deployable
distribution — see [PROVENANCE.md](PROVENANCE.md) for exactly which commits it
was taken from and every way the copy deviates from them.

Practically, that means:

- The three applications are developed in private source repositories and
  mirrored here. Changes made directly to this repository do not reach
  production on their own.
- CI, deployment configuration, and infrastructure definitions are deliberately
  not vendored.
- Some test suites and helper scripts are not part of the snapshot, so a fresh
  clone will not reproduce the full development environment.

## Reporting problems

- **Security vulnerabilities** — do not open a public issue. Follow
  [SECURITY.md](SECURITY.md).
- **Documentation errors, broken links, factual mistakes** — open an issue.
  These are the most useful contributions, and we act on them.
- **Bugs in the application code** — open an issue describing the behavior. We
  will reproduce it against the private source repository and fix it there; the
  fix arrives here with the next snapshot.

## Pull requests

We accept pull requests against documentation in `docs/` and against
repository-level files (README, this file, license notices).

For application code under `backend/`, `frontend/`, and `dashboard/`, please
open an issue first. Because those trees must stay byte-faithful to the
production commits recorded in PROVENANCE.md, a patch applied only here would
break that guarantee. We would rather take your report, fix it upstream, and
credit you.

## Style

Match the surrounding code. Documentation follows the conventions described in
the [README](README.md#documentation): plain-language overview first, each fact
in exactly one place, Mermaid for diagrams.

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

# 5. Administration and Dashboard

The NCTQ Dashboard is the private staff workspace for monitoring Compass,
reviewing saved conversations and quality results, and administering human
access. It is separate from the public Compass chat. Most Compass pages report
what has already happened; they do not rerun a conversation or change the data
that produced an answer. This section is an operational handoff for NCTQ leads,
reviewers, and Starling administrators.

## Purpose and audience

Use the Dashboard to answer three operational questions:

1. Is Compass being used, and what are people asking?
2. Do saved conversations and evaluation results show a problem that needs
   review?
3. Who should have access to the staff workspace?

The Dashboard runs from `dashboard/src/nctqai/`, and its Compass monitoring pages
are under `/compass/*`.

This is an internal operations surface. It is not the public chatbot, an
authoring interface for Compass answers, or the source of truth for role and
authentication rules.

Two neighboring sections cover what this one leaves out: [§6 Hosting, Deployment,
and Security](06-hosting-deployment-security.md) for release controls and
environment boundaries, and [§7 Costs, Accounts, and
Budget](07-costs-accounts-and-budget.md) for account ownership and spending.

## Data boundaries

The Dashboard has four operational boundaries:

| Boundary | Operational meaning |
| --- | --- |
| Compass monitoring | `nctqai` reads canonical `compass.*` records, including saved chat sessions and messages, feedback, scenarios, cases, verdicts, and data-universe summaries. It does not import backend agent internals or regenerate a stored answer. |
| Controlled Compass writes | A flag or flag-status change is sent server-side to the Compass backend. The Dashboard does not write directly to `compass.*` for these actions. The backend owns validation and persistence. |
| Dashboard identity | Human users, OTP codes, and browser sessions live in `nctqai.*`. They are separate from Compass API users and keys. |
| Metric Calculator | `/mc/*` is a separate review workflow. It can write validated metric data and is not part of the Compass observability boundary. |

The saved turn snapshot is the audit artifact for conversation replay. A Dashboard
view shows what was recorded at the time, not a fresh model judgment.

## Human access: email OTP and roles

Dashboard users sign in with a work email and a one-time passcode. The account
must already exist and be active. Requesting a code for an unknown address
returns the same outward response as a known address, so the login form does not
reveal the access list. A successful verification creates a browser session.
Expiry, attempt, rate-limit, cookie, and session settings are configuration,
not promises in this handoff. Consult `dashboard/src/nctqai/config.py` and
`dashboard/src/nctqai/services/auth.py` before troubleshooting or changing them.

The runtime authority for roles and section access is
`dashboard/src/nctqai/models/auth.py`. The four roles are:

| Role | Current operational access |
| --- | --- |
| `viewer` | Read-only Documents and Metric Calculator access, plus the Compass monitoring surfaces. |
| `analyst` | Metric Calculator review actions and Documents, plus the Compass monitoring surfaces. |
| `power_user` | Viewer and analyst areas, supervisory Metric Calculator actions, and the Compass monitoring surfaces. |
| `admin` | All sections, user administration, and admin-only Compass evaluation and operations pages. |

All four roles reach the Compass monitoring surface: Overview, Conversations and
conversation detail, Data Universe, and Flagged Issues. That includes the Flagged
Issues status and dimension controls, which sit behind the same all-roles Compass
section gate. Admin-only, by contrast: Scenarios, Scorecard, Operations, trace
tools, and user administration.

Page navigation is not a security boundary — route guards enforce access. The role
map and route guards in the dashboard source are authoritative; where this prose
differs from them, follow the code.

### Add, change, or deactivate a user

An admin uses **Admin > Users**:

1. To add a user, enter the person's work email, name, and least-privileged
   suitable role. Confirm that the account appears as active. The invite action
   adds access; it does not send a permanent password.
2. To change access, select the new role. Authorization is evaluated from the
   current user record on requests, so the new role does not require a new
   account.
3. To remove access, deactivate the user. Deactivation destroys that user's
   open Dashboard sessions. Do not delete the user record because it is part of
   the audit trail. Reactivate the same record if access is restored later.

The UI prevents an admin from editing their own account and prevents removal or
deactivation of the last active admin. For initial seeding, bulk changes, or
lockout recovery, use the approved operator procedure rather than an improvised
database update.

## Programmatic access: Compass API keys

Compass API authentication is the second door. Scripts, the frontend, the eval
harness, and server-to-server Dashboard actions can send a bearer API key to the
Compass backend. This does not create a Dashboard browser session and a
Dashboard role does not automatically mint or grant an API key.

The backend stores a hash, a safe key identifier, ownership metadata, optional
expiry, and revocation state. It never needs to store or display the full token
after minting. On each authenticated request, the backend rejects expired or
revoked keys and resolves admin scope from the key owner's current
`compass.users.is_admin` value. The operative implementation is
`backend/src/compass_backend/api/auth.py`.

### Safe rotation and revocation

Use an overlap rotation so service access can be tested before the old key is
disabled:

1. Identify the key owner, purpose, environments, and every service that uses
   the key. Record only the safe key identifier in tickets and logs.
2. Mint a replacement through the approved environment-specific process. Put
   the full value directly into the approved secret store. Never paste it into
   this repository, a ticket, chat, command transcript, or Dashboard field.
3. Update one consumer at a time. Restart or redeploy as required, then verify an
   authenticated request and confirm usage is attributed to the replacement's
   safe key identifier.
4. Soft-revoke the old key by setting its revocation timestamp through the
   approved administrative process. Never hard-delete an API-key row.
5. Confirm the old key is rejected and the replacement still works. Retain the
   old row for audit history.

If a key may be exposed, revoke it first and accept the brief outage if a safe
overlap cannot be established. Do not weaken API-key authentication to recover a
consumer. Production database writes are prohibited from this handoff workflow;
use the approved credential and deployment runbooks and validate in staging
first.

## Core workflows

### Conversations

Use **Compass > Conversations** to search or filter persisted conversations.
Operators can search message text, paste a session identifier, narrow by date or
feedback, and open a stable conversation detail view. The detail should reflect
the saved prompts, answers, tables, citations, feedback, and available verdicts.

When investigating a bad answer, preserve the session identifier and the exact
turn. Do not ask Compass to recreate the answer and treat the new output as
evidence. If engineering investigation is required, hand off the session or
trace reference through the documented debugging workflow.

### Quality and scorecard

The admin-only **Scorecard** reads the verdict ledger and groups evaluation
results by quality dimension, case, sweep, and trial. A `pass`, `fail`, or
`error` outcome has a specific meaning in the ledger. Do not combine product
failures, judge or harness errors, missing traces, and skipped checks into one
failure count. [§4 Quality & Evaluation](04-quality-and-evaluation.md) describes
the evidence expected before a change ships.

The scorecard is a review surface, not the place to edit criteria, repair data,
or rerun production conversations. Evaluation changes and replays belong to the
Compass evaluation workflow.

### Key metrics and how they are calculated

**Compass > Overview** is the usage surface. Every tile on it is a specific
calculation over saved conversations, and several of them are easy to misread if
you do not know the denominator. This section is the definition list; the
implementation is `dashboard/src/nctqai/services/compass_stats.py` and
`routes/compass/overview.py`, which remain authoritative.

Two conventions run through the whole page. First, **the unit is usually the
conversation, not the message** — most rates divide by sessions. Second, **every
people tile honors the active date-range pill** (7 days, 30 days, all time), so
two numbers on the same screen always describe the same window.

#### People

Three tiles count people, and they are deliberately not the same number.

| Tile | Calculation | What to watch for |
| --- | --- | --- |
| **Unique visitors** | Site-level unique visitors, for people who viewed Compass on Pathfinder. Read from Google Analytics where it is configured, which de-duplicates by client ID across all time and so answers every window | A self-hosted Umami instance is still the fallback where GA is not configured, and it de-duplicates only *within* a calendar month — its salt resets on the first. On that path the tile shows no number for the all-time range, because a monthly regular would be counted roughly twelve times a year. Retiring the second tool is tracked in [§9](09-known-issues-and-limitations.md#two-analytics-tools-measure-the-same-thing-google-analytics-should-be-the-only-one). |
| **Chat users** | `COUNT(DISTINCT visitor_id)` over sessions in the window — distinct people who actually started a conversation | This is the honest "how many real users" number: chat-level and de-duplicated with no monthly reset. It counts only visitors carrying a pseudonymous ID minted by the Pathfinder embed, so traffic that reaches the chat outside the embed is not counted. |
| **Returning users** | Of those identified visitors, how many started **2 or more** conversations in the window | Chat-level, and a subset of Chat users — not a subset of Unique visitors. |

When no visitor is identified in a window, the Chat users and Returning users
tiles show *"Counting begins at launch"* rather than a bare zero, because a zero
would read as "nobody uses Compass" when the real meaning is "no IDs were minted
in this window." The visitor ID itself is pseudonymous and carries no personally
identifying information; see [§8](08-technical-reference.md#pathfinder-integration).

#### Engagement

| Tile | Calculation |
| --- | --- |
| **Sessions** | `COUNT(*)` of conversations in the window. Carries a period-over-period trend caption only when a prior window of equal length exists and had a non-zero count — otherwise no trend is shown rather than a fabricated one. |
| **Avg user questions** | User messages ÷ sessions, to one decimal. Counts user messages only, not assistant replies. |
| **Multi-turn rate** | Percentage of sessions with **3 or more** user questions. A 2-or-more figure is also computed but deliberately has no tile, so there is one canonical multi-turn number. |
| **Artifact coverage** | Share of sessions that produced a table, a CSV export, or a chart. Each tile links into a filtered Conversations view, so the number is auditable by clicking it. |

#### Feedback

Thumbs up, thumbs down, and not rated are all percentages **of sessions**, not
of assistant messages. A conversation counts as thumbs-down if it contains any
down-rating, as thumbs-up if it has at least one up-rating and no down-ratings,
and as unrated otherwise. So the three bars describe conversations and sum to
the session total. A high "not rated" share is a statement about rating
adoption, not about answer quality.

#### What conversations covered

Top policy areas, top districts, and top states are each counted **once per
conversation**, drawn from the metric and district context Compass saved with
the turn. A conversation that asks about Denver six times contributes one to
Denver. These are the top three in each category, so they are a read on what
people ask about, not a complete distribution.

#### Reading these numbers responsibly

- A tile marked **"Under construction"** names a metric that is not live yet.
  It is not a zero.
- Usage metrics and quality metrics answer different questions. Nothing on the
  Overview page is evidence that answers were correct; that is the
  [scorecard's](#quality-and-scorecard) job, on its own denominators.
- Site-level web analytics and chat-level metrics
  (everything derived from `compass.*`) will not reconcile to each other, and
  are not expected to. Do not report a ratio between them.

### Data Universe

Use **Compass > Data Universe** to inspect what the canonical Compass schema can
currently describe, including inventory, sources, coverage, and available-year
summaries. It reports the loaded universe. It does not ingest a source, correct
a policy answer, or prove that an individual chat answer selected the right row.

If a value is missing, first determine whether the problem is source ingestion,
canonical schema coverage, catalog or execution selection, or Dashboard
presentation. Route the issue to that owner instead of repairing it in the
Dashboard.

### Flagging and triage

From a conversation detail, submit a flag with a concise observation and the
most appropriate quality dimension when known. The Dashboard sends the flag to
the Compass backend, which creates the persisted report. A successful flag then
appears in **Flagged Issues**.

In Flagged Issues, reviewers filter the queue, open the linked conversation,
assign or correct the dimension, and move the item through the available triage
statuses. A whole-conversation flag may not have a case identifier, so it may
have a conversation link without a runnable case link. Do not invent a case ID
to fill that gap. Promotion to a GitHub issue and creation of a regression case
belong to the issue-intake and scenario-management workflows, not to a casual
status change.

## Routine administration checks

Perform these checks on the team's agreed cadence and after authentication,
deployment, or data changes:

- Sign in through the normal OTP flow with a non-admin test account and confirm
  that expected sections load and admin-only routes deny access.
- Review active users, roles, last-login dates, and the number of active admins.
  Deactivate departed staff promptly.
- Check that OTP delivery works without exposing whether an address is enrolled.
  Investigate rate limiting, mail relay configuration, and application logs
  before repeatedly requesting codes.
- Confirm the Dashboard can read current `compass.*` data and that Conversations,
  Data Universe, and Flagged Issues agree on recent records.
- Review open flags and aging triage items. Ensure each actionable product issue
  has an owner and stable evidence.
- Review the latest scorecard sweep separately from live-user feedback. Confirm
  that errors are classified before reporting a quality rate.
- Review API-key safe identifiers, owners, purpose, expiry, revocation state,
  last use, and unexpected request activity. Rotate stale or over-broad keys.
- Confirm authentication is enabled outside local development. Never use the
  local auth bypass as a staging or production recovery mechanism.

## Failure modes and ownership

| Symptom | First checks | Owning boundary |
| --- | --- | --- |
| No OTP email arrives | Confirm the account is active, wait for delivery, check rate-limit and mail logs, then verify SMTP configuration without exposing credentials. | Dashboard identity and mail delivery |
| OTP works but a page is forbidden | Compare the user's current role with `User.can_access` and the route's shared guard. Do not add a one-off route exception. | Dashboard authorization |
| Deactivated user still appears signed in | Confirm deactivation succeeded and the user's Dashboard sessions were destroyed. Distinguish browser sessions from Compass API keys. | Dashboard user administration |
| Dashboard page is empty or stale | Check database connectivity, the selected date range, the relevant saved rows, and application logs. Do not regenerate a conversation to fill a display gap. | Dashboard read model, or upstream persistence if rows are absent |
| Conversation replay differs from what the user saw | Inspect the saved turn snapshot and replay renderer. If the snapshot itself is incomplete, hand off to Compass persistence. | Dashboard replay when saved data is intact; Compass backend persistence otherwise |
| Flag submission or status update fails | Check Dashboard-to-backend connectivity, configured server credential, backend response, and report identifiers. Do not write `compass.case_reports` directly as a shortcut. | Compass backend report API |
| Scorecard shows `error` or no trials | Separate harness, judge, trace, contract, and product outcomes before escalation. | Evaluation pipeline and verdict ledger |
| Data Universe lacks expected coverage | Verify source arrival and canonical tables before checking the Dashboard query. | Data ingestion and `compass.*` data model first; Dashboard presentation second |
| API returns unauthorized | Check that the consumer sent the intended environment's full key, that the key is unexpired and unrevoked, and that the owner still has the required scope. | Compass API authentication |
| Metric Calculator data looks wrong | Use the Metric Calculator review and ingestion ownership path. Do not treat the Compass observability pages as its write interface. | Metric Calculator or data ingestion |

## Dashboard versus Metric Calculator and ingestion

Two applications share the `nctqai` service but have different jobs:

- The **Compass Dashboard** under `/compass/*` monitors persisted Compass
  activity, feedback, data coverage, reports, and evaluation evidence. Its
  normal posture over `compass.*` is read-only, with controlled report actions
  delegated to the Compass backend.
- The older **[Metric Calculator](reference/metric-calculator.md)** under
  `/mc/*` is an analyst workflow for reviewing AI-suggested policy answers and
  writing validated metric data. It is part of the policy-data preparation and
  review history, not the Compass conversation-monitoring pipeline.

The broader data-ingestion project prepares and loads source policy data. It
owns source acquisition, transformation, and loading. The Dashboard can reveal
the resulting coverage but does not replace that pipeline. Shared deployment or
database infrastructure does not make these responsibilities interchangeable.

## What this document does not contain

No personal access roster, email addresses, passwords, API-key values, connection
strings, secret-store locations, production mutation commands, or live URLs that
are not already governed elsewhere. Use the approved credential, database,
deployment, and incident runbooks for those details. Share safe key identifiers
and stable record IDs only when an operator needs them to investigate.

# 9. Known Issues & Limitations

> **An honest, current list.** What is broken, worked around, or deliberately out
> of scope, kept as a running punch list. This doc goes stale fastest of any
> section here, so any future addition should carry an owner and a review date.
> **Owner:** Compass team. **Next review:** 2026-09-15.

This section covers:

- Known issues with workarounds in place.
- Items flagged as needing fixes but not yet resolved.
- Known failure patterns from evaluation, stated in plain language (e.g. cases where
  the automated graders are stricter than the product deserves).
- Product limitations by design: what Compass intentionally does not do. It answers
  only from NCTQ's reviewed data, it does not browse the web, and it does not offer
  opinions beyond NCTQ's published positions.

## Known limitations

### The final check catches added facts, not dropped ones

After the writer polishes an answer's wording, the final check verifies that
nothing was **added or changed**: every number in the polished text must already
exist in the sealed facts, and every citation marker must be one the system built
(see [§2 Generation](02-product-and-answer-flow.md#generation-facts-first-phrasing-second)).
What it does not verify is the mirror image: that everything important was
**kept**. The check can prove the writer told no lies; it cannot prove the writer
told the whole truth.

The exposure is narrow, because the highest-stakes content is not loose prose the
writer could drop: the data tables, the Sources blocks, and the canonical coverage
caveats are *sealed*, marked immutable, with rewrites that touch them rejected.
What remains exposed is a fact that lives only in free prose (a nuance in the lead
sentence, say, that isn't also in a table or a sealed caveat); a rewrite could thin
that out and nothing would catch it.

Why it was built this way: catching additions is mechanically precise (extract
the numbers, compare against the allowed set). Catching omissions requires first
defining which facts are *required* in each particular answer, a much harder
contract. Version one shipped the precise half and sealed the most important prose
as the mitigation. The proper fix is the fact-coverage gate described in
[the writer improvement below](#a-writer-that-composes-from-the-full-data-behind-a-fact-coverage-gate).

### NCTQ policy guidance is a managed Markdown stopgap

Compass serves NCTQ positions, research rationales, and exemplary policies from
the reviewed [NCTQ Policy Content Markdown Stopgap](../backend/content/nctq-policy/README.md).
The files are parsed and rendered deterministically, so the model does not invent
this guidance or its citations. But the library began as a compact, May 2026
snapshot of the source documents; in particular, the research-rationale document
contains more narrative and source-backed detail than has been migrated here.

The next step is a structured migration of that richer rationale material into
topic and subtopic entries, with its source references preserved. Until that work
is complete, Compass can safely present the reviewed guidance in the library, but
it should not imply that the library contains every research rationale NCTQ has
prepared.

### A very long conversation can outgrow its working memory

Compass is designed to follow a conversation, not to remember a person across
separate chats. Its typed memory keeps the recent results, goals, and unresolved
questions that a follow-up needs. But the full message history still grows with
every turn. A sufficiently long thread can therefore become slower or eventually
run into the model's context limit.

This is a reliability limitation, not a reason to make up a continuation. If the
thread becomes too large, Compass should preserve the recent, structured context
and summarize or retire older message history. That compaction policy—how much to
retain, what can be summarized, and how to prove a follow-up still works—has not
yet been implemented. Owner: Compass backend. Review: 2026-09-15.

### A model-provider outage can interrupt Compass

Compass uses several model roles: one plans the data request, others help phrase
answers, resolve close matches, and evaluate quality. Today those roles depend on
a configured model provider. If that provider has a broad outage, a user turn can
fail even though Compass's data and deterministic query system are available.

The safe remedy is not to silently substitute an untested model. Compass needs a
tested fallback for the roles where a fallback can preserve its grounding rules,
a visible marker when that degraded path was used, and a plain, distinct message
when no provider is available. Owner: Compass backend. Review: 2026-09-15.

## Improvements under way

### LLM spend is split between the application estimate and Gateway billing

Compass currently routes its LLM calls through the
[Pydantic AI Gateway](https://gateway.pydantic.dev/). The Gateway provides a
native spending view, while Compass also records per-call token usage and
model-price estimates in `compass.llm_usage`. Those sources answer related but
different questions: the application ledger explains token consumption and
estimated cost, while the Gateway or provider billing record is authoritative
for what is charged.

The remaining limitation is operator experience. There is not yet one simple
Compass-facing view that combines production model calls, input/output/cache
tokens, estimated cost, trends, spending limits, and reconciliation to the
Gateway ledger. A more user-friendly production spend dashboard is tracked in
[NCTQ closeout issue #33](https://github.com/Starling-Strategy/compass/issues/33).
The issue already tracks the Gateway handoff and documentation work; the
operator-friendly dashboard can be added as a follow-up there.
Until that work is complete, operators should review the application estimate
and Gateway Spending view together. Owner: TBD. Review: during the next
operations-dashboard pass.

### An answerable question can still receive a generic clarification

Compass has a deliberate recovery ladder: when it cannot answer directly, it
should offer a grounded alternative, a useful clarification, or a specific honest
explanation. Most of that system is now in place. A small set of different prompt
shapes can still fall through to a generic “please rephrase” response even though
the question contains enough information to answer or clarify productively.

That is more than a wording issue: a generic clarification discards useful intent
the user already supplied. Each remaining shape needs a literal regression case,
and the fallback should name the real constraint or offer a grounded next step.
Owner: Planner and catalog pipeline. Review: 2026-09-15.

### The planner picks a query shape before it sees the data

When Compass plans an answer, the planner must commit up front to one of a fixed
set of query shapes (a lookup, a ranking, a count, a trend, and so on) before it
has explored what data actually exists for the question
(see [§2 Planning](02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan)).
Most questions fit a shape cleanly. But a question that straddles two shapes, or
where the right shape only becomes obvious after a look at the available data, can
land worse than the data would support: a clarifying question where a direct answer
was possible, or an answer forced into an awkward shape.

The improvement being worked on is to let the planner **explore first**: browse the
catalog of districts, metrics, and topics with read-only tools, then compose the
plan from what it finds instead of guessing blind. What runs the query does not
change: the same typed, deterministic operations still fetch every fact. The
grounding rules still hold too, so the planner still cannot supply an ID, a number,
or a citation. And the change ships only behind its safety net: the planner gets more
freedom on a given kind of question only after the verification (grounding guards
and evaluation coverage) for that kind of question is in place.

### A writer that composes from the full data, behind a fact-coverage gate

Today's writer is deliberately kept on a short leash: it sees only the finished
answer skeleton (the "sealed brief") and works as a line editor: rephrase this,
don't touch that
(see [§2 Generation](02-product-and-answer-flow.md#generation-facts-first-phrasing-second)).
That caps answer quality: the writer can't decide structure or emphasis, only
polish template output.

The target flips it. The writer would see the **full data artifact** (all the
fetched rows, the coverage information, the metadata) plus its instructions, and
compose the answer from scratch: a real writer instead of a polisher, free to
choose what to lead with and which observation the data genuinely invites.

That freedom is only safe with stronger verification to match, and that is the
**fact-coverage gate**: from the same data artifact, derive the checklist of facts
this answer *must* contain (the values asked for, the coverage caveats, the year
labels) and verify each one is present in the final text. Combined with today's
gate, both directions are covered: nothing added that isn't in the artifact, and
nothing required missing from the answer. Fail either gate and the same fallback
fires as today: ship the deterministic skeleton. The coverage gate is the keystone
piece not yet built; until it exists, the writer stays muzzled and the sealing
does the protecting (see
[the limitation above](#the-final-check-catches-added-facts-not-dropped-ones)).

Same principle as the planner entry: freedom expands only where verification
already covers it.

### Production promotion is not yet a fully mechanical proof of safety

Compass has a disciplined local validation process, and post-launch fixes have
been checked against real scenarios. The remaining gap is making every promotion
prove the same essentials in the same order: database changes before dependent
code, a passing local gate, literal regression prompts, and a fresh-session probe
on the build being promoted.

Without that repeatable release evidence, a familiar set of staging scenarios can
pass while a newly asked production question exposes a missing shape or an
environment ordering problem. The improvement is a small release checklist with
recorded results, not a larger deployment system. Owner: Release engineering.
Review: before the next production promotion.

### Living documentation can drift from the running system

Compass has detailed material on its architecture, evaluation, and operating
practice. Some of that material can still retain a moved code path or a link whose
target was archived, which makes the right workflow harder to find when someone
is investigating a problem.

The fix is straightforward: maintain an offline check for links and literal code
paths in living documentation, while deliberately exempting historical audits and
plans. Owner: Documentation. Review: 2026-09-15.

### Some voice and copy concerns need a fresh replay before a fix is chosen

Several post-launch reports identified language that was too technical, too vague
about the review year, or insufficiently responsive to a user's real question.
Some of the original examples no longer reproduce after later fixes. The remaining
work should begin with a current replay, then separate confirmed behavior from
historical evidence.

The standard is simple: Compass names the relevant review year and limitation in
plain language, acknowledges a prioritization question instead of changing the
subject, and never asks a user to understand internal pipeline vocabulary. Owner:
Answer experience. Review: 2026-09-15.

## Evaluation program

The evaluation program's own known issues and open questions, moved here from
[§4](04-quality-and-evaluation.md) so all open items live on one punch list.
Evidence for each is in the
[2026-08-12 evaluation results](reference/2026-08-12-evaluation-results.md).
Owner: unassigned — that gap is itself an item below. Review: at the next
ledger export.

### Run-to-run comparability is the program's weakest property

The grader and the product changed together for most of the ledger's history:
122 distinct criterion-set fingerprints and 111 distinct product builds
across 413 sweeps. Most score movements therefore cannot be attributed
cleanly to the product or the criteria — the two-lens pair and the
trajectories in the evidence file are the concrete demonstrations. The fix is
procedural, not clever: freeze the criterion set, run one full
seven-dimension sweep on a single build, and declare that the baseline board.
Every later change then gets the three-way replay from
[§4 section 3](04-quality-and-evaluation.md#3-how-compass-runs-evaluations)
(old code / old judge, old code / new judge, new code / new judge) so
evaluator drift and product change are estimated separately instead of argued
about.

### Criteria fire on cases they were never scoped to

Regression criteria written for one scenario are sometimes attached by the
evaluation classifier to unrelated cases, where they record failures that are
noise, not signal — visible in the ledger as post-fix "failures" of criteria
whose home cases pass cleanly. Scoping guards have landed for specific
criteria; the general fix (a criterion declares its applicable scenarios and
the classifier honors that declaration) is not yet systematic.

### Five dimensions sit below their configured targets on the strictest lens

Selection (47% on the holistic judge, 65% on structured checks) and Data
Fidelity (62%) carry the largest gaps as of the 2026-06-22 pinned sweeps. The
failing criteria are themselves the worklist: each firing criterion names a
case, a rule, and a recorded reason. Separately, the targets themselves
(95–99%) should be re-confirmed or revised now that a stricter instrument
exists — a target set against a gentler grader is not automatically the right
bar for a harsher one.

### The judges' pass side is largely unaudited

The 2026-06-21 review verified the fail side (most recorded failures are
real) and found one recall hole — passed refusals — which now has a dedicated
deterministic check. But "a passing verdict means the answer was good" has
only been spot-checked, never systematically audited. In a related failure mode,
a criterion can exist but be attached to the wrong cases or have nothing to grade,
so it records little or no signal while a reviewer sees a real defect.

The remedy is two-sided: periodically sample passing verdicts with human review,
and audit high-level criteria against known reviewer failures to prove that they
both apply and catch the behavior they claim to cover. A score is useful only when
its numerator, denominator, and applicability are visible. Owner: Evaluation.
Review: at the next ledger export.

### Observability gaps show up inside otherwise-fixed cases

The worked example in [§4 section 6](04-quality-and-evaluation.md#6-from-a-failure-to-a-fix)
passes its behavior criteria but still fails its trace-ID check in the same
sweep: correct but unobservable. These trace-missing verdicts are tracked as
their own axis rather than folded into product failures, and they need an
infrastructure fix, not a product one.

### The scenario library has no named owner or review cadence

The library grew from 326 active scenarios (June 9 audit) to 390 (August 12
export) without an owner, review cadence, or retirement policy. Duplicate,
stale, or mis-scoped cases degrade every number downstream. Someone has to
own the benchmark the way someone owns the data pipeline.

### Evaluation evidence is still harder to reuse than it should be

Compass keeps valuable evidence in several places: the evaluation ledger, dated
reports, scenario definitions, and the code that calculates the scorecard. The
recent results document brings much of that story together, but the underlying
evidence still needs a repeatable, privacy-conscious export with its date, build,
grader, denominator, and redaction rules recorded alongside it.

That package would make it possible to answer a basic client and team question
without archaeology: what did Compass test, what did the result mean, and which
remaining findings are product gaps rather than evaluator or infrastructure noise?
Owner: Evaluation. Review: at the next ledger export.

### Staff feedback is not yet wired into the evaluation ledger

Staff can flag live conversations from the Dashboard, and those flags are
leads for investigation — but there is no defined path from a flag to a saved
scenario, case, and criterion. Until that link exists, field observations
depend on someone manually carrying them into the library, and some will be
lost.

### Whether an answer-time judge should ever block a response is undecided

Background quality judging is diagnostic by design: it records verdicts after
the response ships and does not edit or block it. Whether any future judge
should become a pre-send gate is an open product decision, deliberately not
made yet — a blocking gate raises latency, adds a new failure mode, and
requires far higher judge precision than a diagnostic one.

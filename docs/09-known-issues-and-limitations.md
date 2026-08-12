# 9. Known Issues & Limitations

> **Stub: mostly to be drafted.** An honest, current list of what is broken, worked
> around, or deliberately out of scope, kept as a running punch list in this file
> for now. This doc goes stale fastest, so it carries an owner and a review date
> once drafted. The entries under [Known limitations](#known-limitations) and
> [Improvements under way](#improvements-under-way) are live.

Planned contents:

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

## Improvements under way

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

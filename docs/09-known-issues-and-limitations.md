# 9. Known Issues & Limitations

> **Stub — mostly to be drafted.** An honest, current list of what is broken, worked
> around, or deliberately out of scope. This doc goes stale fastest, so it carries
> an owner and a review date once drafted. The first entry, under
> [Improvements under way](#improvements-under-way), is live.

Planned contents:

- Known issues with workarounds in place.
- Items flagged as needing fixes but not yet resolved.
- Known failure patterns from evaluation, stated in plain language (e.g. cases where
  the automated graders are stricter than the product deserves).
- Product limitations by design: what Compass intentionally does not do — it answers
  only from NCTQ's reviewed data, it does not browse the web, and it does not offer
  opinions beyond NCTQ's published positions.

## Improvements under way

### The planner picks a query shape before it sees the data

When Compass plans an answer, the planner must commit up front to one of a fixed
set of query shapes — a lookup, a ranking, a count, a trend, and so on — before it
has explored what data actually exists for the question
(see [§2 Planning](02-product-and-answer-flow.md#planning-intent-becomes-a-typed-plan)).
Most questions fit a shape cleanly. But a question that straddles two shapes, or
where the right shape only becomes obvious after a look at the available data, can
land worse than the data would support: a clarifying question where a direct answer
was possible, or an answer forced into an awkward shape.

The improvement being worked on is to let the planner **explore first**: browse the
catalog of districts, metrics, and topics with read-only tools, then compose the
plan from what it finds instead of guessing blind. What runs the query does not
change — the same typed, deterministic operations still fetch every fact, and the
grounding rules still hold: the planner still cannot supply an ID, a number, or a
citation. And the change ships only behind its safety net: the planner gets more
freedom on a given kind of question only after the verification (grounding guards
and evaluation coverage) for that kind of question is in place.

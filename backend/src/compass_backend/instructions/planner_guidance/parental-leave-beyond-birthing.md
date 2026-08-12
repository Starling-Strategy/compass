## Parental leave beyond birthing parent

When the user asks about *paid parental leave* combined with phrasing that signals lanes **beyond birthing parent** — *"non-birthing"*, *"adoptive"*, *"foster"*, *"beyond just birthing parent"*, *"more than just birthing"*, *"other than birthing parent"* — they want NCTQ's eligibility coverage across all parent lanes.

Emit:

- `route="execute"`
- `query_plan.operation="lookup"`
- `query_plan.selection.scope="all_covered_districts"`
- `query_plan.metrics` (exact names, in this order):
  - `MetricSpec(name="Who is eligible for paid parental leave?", role="primary")`
  - `MetricSpec(name="Number of paid parental leave days beyond sick days granted to non-birthing parent", role="comparison")`
  - `MetricSpec(name="Number of paid parental leave days beyond sick days granted to foster parent", role="comparison")`
  - `MetricSpec(name="Number of paid parental leave days beyond sick days granted to adoptive parent", role="comparison")`
- `query_plan.filters` (apply the user's "beyond birthing parent" constraint — do not return a data dump):
  - `FilterSpec(field="Who is eligible for paid parental leave?", operator="in", value=["Non-birthing parent", "Foster parents", "Adoptive parents", "Other"], kind="metric_value")`
- `query_plan.output.format="table"`

Use the metric names exactly as written above. They include the full phrase "Number of paid parental leave days beyond sick days granted to X parent" — catalog resolution depends on the exact wording.

The eligibility metric stores a comma-joined cell of parent categories (e.g.
`"Birthing parent, Non-birthing parent (gender not specified), Foster parents, Adoptive parents"`).
The `in` filter keeps only districts whose eligibility lists at least one
category beyond the birthing parent and drops `Not applicable`, not-reviewed,
and birthing-parent-only rows. Mirror the filter `field` on the primary
`MetricSpec.name` exactly so one catalog call resolves both. The filter
`value` lists the category labels (not the metric name) — use the short
prefixes above so `"Non-birthing parent"` matches the gendered variants and
`"Other"` matches the `Other: …` placements.

### Required phrase combination

All three of these signals must be present to emit this shape:

1. *"paid parental leave"*
2. *"birthing parent"* (the contrast point — the user is asking about lanes *beyond* this one)
3. One of: *"non-birthing"*, *"adoptive"*, *"foster"*, *"beyond just"*, *"more than just"*, *"other than"*

If only one or two are present, fall through to the planner's normal recognition — don't force the 4-metric shape.

### Negative parental-leave filter

When the user asks for districts that **lack** a paid parental leave policy — phrasing like *"no parental leave"*, *"without parental leave"*, *"don't offer parental leave"*, *"districts that do not offer paid parental leave"* — do **not** emit a clarify. This is a *whether-offered* question, not an *eligibility-category* question, so filter the boolean offered metric rather than the eligibility categories: emit a typed boolean-`False` metric filter on the offered metric.

<example>
User: "Districts with no parental leave policy."
Plan shape: operation="lookup", selection.scope="all_covered_districts", metrics=[MetricSpec(name="Does the district offer paid parental leave?")], filters=[FilterSpec(field="Does the district offer paid parental leave?", operator="equals", value=False, kind="metric_value")].
</example>

<example>
User: "Which districts don't offer paid parental leave?"
Plan shape: same as above — operation="lookup", a single boolean offered metric, and a single `equals False` filter on that same metric field.
</example>

Use the metric name `"Does the district offer paid parental leave?"` exactly, on both the `MetricSpec.name` and the `FilterSpec.field`. The filter value is the boolean `False`, never the string `"no"` or `"none"`.

### Counter-examples

- "Paid parental leave for birthing parent" → single-lane, single-metric lookup
- "How many districts offer paid parental leave?" → count, not lookup
- "Compare paid parental leave between Denver and Chicago" → comparison

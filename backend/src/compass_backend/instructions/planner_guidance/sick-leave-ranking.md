## Texas paid sick / leave day ranking

When the user asks to **rank or compare paid sick leave (or paid leave) days across districts** — phrasing like *"highest"*, *"most"*, *"lowest"*, *"least"*, *"rank"*, *"sort"*, *"compare"* combined with *"sick leave days"* or *"paid leave days"* — and the request is scoped to **Texas** (where the sick/personal-leave distinction matters), emit the governed multi-metric ranking shape directly. Do not fall through to a metric-disambiguation clarify.

Emit:

- `route="execute"`
- `query_plan.operation="rank"`
- `query_plan.selection.scope="state"`, `query_plan.selection.states=["TX"]`
- `query_plan.metrics` (exact names, in this order):
  - `MetricSpec(name="Maximum number of annual paid sick days", role="primary")`
  - `MetricSpec(name="Number of paid leave days granted in first year of employment if district does not distinguish between paid sick and personal leave days", role="comparison")`
  - `MetricSpec(name="Total number of paid sick and paid personal leave days granted in first year of employment if district does differentiate between personal and sick leave", role="comparison")`
- `query_plan.sort.field="Maximum number of annual paid sick days"`
- `query_plan.sort.direction`:
  - `"asc"` when the user asks for *lowest* / *least* / *minimum*
  - `"desc"` otherwise (the default — *highest* / *most*)
- `query_plan.limit.kind="all"`
- `query_plan.output.format="table"`

Use the metric names exactly as written above — the second and third comparison metrics carry the full "does not distinguish" / "does differentiate" wording that catalog resolution depends on. Texas districts split into two reporting conventions (one combined sick+personal bucket vs. a differentiated bucket), so the primary "Maximum number of annual paid sick days" metric needs both comparison metrics alongside it to read correctly.

### Required signals

All of the following must be present to emit this shape:

1. *"sick leave"* (or *"paid leave"*) **and** a *"day"* signal
2. A Texas scope signal: *"Texas"* or *"TX"*
3. A ranking / comparison verb: *"highest"*, *"most"*, *"lowest"*, *"least"*, *"rank"*, *"ranked"*, *"ranking"*, *"sort"*, *"sorted"*, *"compare"*

If the request is not Texas-scoped, or carries no ranking verb, fall through to the planner's normal recognition — don't force the 3-metric ranking shape.

### Examples

<example>
User: "How many paid sick leave days do teachers in Texas districts get, ranked lowest to highest?"
Plan shape: operation="rank", selection.scope="state", selection.states=["TX"], metrics=[primary "Maximum number of annual paid sick days", two comparison metrics above], sort.field="Maximum number of annual paid sick days", sort.direction="asc".
</example>

<example>
User: "Which Texas districts give the most paid sick days?"
Plan shape: operation="rank", selection.scope="state", selection.states=["TX"], same three metrics, sort.direction="desc".
</example>

### Counter-examples

- "How many paid sick days does Austin ISD offer?" → single-district lookup, not a ranking.
- "Rank California districts by paid sick days" → not Texas-scoped; use normal recognition.
- "What's the average teacher salary in Texas?" → not a sick-leave request.

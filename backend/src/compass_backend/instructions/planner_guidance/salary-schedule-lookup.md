## Named-district salary schedule

When the user asks for a named district's "salary schedule" — *"What's Denver's salary schedule?"*, *"Show me Denver Public Schools' salary schedule"*, *"Pull up the salary schedule for Chicago"* — they want a default overview of the salary range, not a single metric.

Emit:

- `route="execute"`
- `query_plan.operation="lookup"`
- `query_plan.selection.scope="named_districts"`
- `query_plan.selection.districts=[<the district name the user named>]`
- `query_plan.metrics` (exact names, in this order):
  - `MetricSpec(name="BA starting salary", role="primary")`
  - `MetricSpec(name="MA starting salary", role="comparison")`
  - `MetricSpec(name="maximum teacher salary", role="comparison")`
- `query_plan.output.format="table"`

Use the literal district phrase the user gave (e.g. "Denver", "Denver Public Schools", "Chicago Public Schools"). Catalog resolution will canonicalize it.

### Counter-examples — do not use this shape

- "What's the highest starting salary?" → rank, not named-district lookup
- "How does Denver's salary schedule compare to Chicago's?" → comparison, not single-district lookup
- "Denver's master's salary" → single-metric lookup (use the named metric directly, not the 3-metric overview)
- "Denver's salary schedule for new hires" → user named a sub-shape; clarify or pick the most specific metric

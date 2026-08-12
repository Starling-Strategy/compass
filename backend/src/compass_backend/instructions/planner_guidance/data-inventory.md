## Data inventory request

When the user asks *"what data do you have about [X]"* (or *"what info do you have on X"*, *"what information do you have for X"*), they want a directory of available data — not a count, comparison, or ranking.

### District inventory (most common shape)

If X is a district phrase (contains "district" or "public schools" — *"Philadelphia school district"*, *"Aurora Public Schools"*), emit a profile-field lookup for that district:

- `route="execute"`
- `query_plan.operation="profile_lookup"`
- `query_plan.selection.scope="named_districts"`
- `query_plan.selection.districts=[<the district phrase the user gave>]` (strip a trailing `'s` if present)
- `query_plan.profile_fields` (in this order — use these exact field names):
  - `ProfileFieldSpec(name="enrollment")`
  - `ProfileFieldSpec(name="teachers_fte")`
  - `ProfileFieldSpec(name="pupil_teacher_ratio")`
  - `ProfileFieldSpec(name="number_of_schools")`
  - `ProfileFieldSpec(name="locale_text")`
  - `ProfileFieldSpec(name="frpl_pct")`
- `query_plan.metrics=[]`
- `query_plan.output.format="table"`

### Topic / bundle inventory

If X is a topic or metric bundle (e.g. *"performance pay"*), use normal catalog metric recognition. If ambiguous, route to clarify with available bundle options. Do not invent a profile_lookup for non-district subjects.

For *"performance pay"* specifically, answer as a governed capability inventory, not a generic clarification:

- `route="direct"`
- `direct_response.reason="performance pay data inventory"`
- `direct_response.message` should say that Compass has reviewed district data for:
  - `Minimum annual performance pay bonus, if eligible`
  - `Maximum annual performance pay bonus, if eligible`
- The message may tell the user they can ask Compass to rank districts by maximum annual performance pay bonus, filter for substantial bonuses, compare named districts, or narrow results by state or region.
- Do not include district rows or dollar amounts in this inventory response; those require `route="execute"`.

### Counter-examples — do not use the district-inventory shape

- "How many districts have data on X?" → count, not inventory. When X is a topic, route to `operation="count"`, `count_kind="topic_coverage_count"`, `metrics=[MetricSpec(name="<topic>")]` (see the `topic_coverage_count` count_kind in the planner base instructions).
- "Compare data on X between Denver and Chicago" → comparison, not inventory
- "What is Denver's enrollment?" → single-field lookup (use the named field directly, not the 6-field overview)

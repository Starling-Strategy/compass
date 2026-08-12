<guidance topic="profile-sort-metric-display">
<title>Profile-sort metric display</title>
<scope>Applies when the user wants to rank or select districts BY a profile field (such as enrollment, FRPL, or pupil-teacher ratio) but DISPLAY a governed policy metric (such as a salary, stipend, premium, leave, or benefit) for those districts.</scope>

- Keep the operation executable; use `operation="rank"` with a selection-phase `profile_field` sort step for the profile field.
- KEEP the user's policy metric as the primary `MetricSpec` to DISPLAY. Do NOT drop it and rank by the profile field's own value — the profile field only chooses and orders which districts appear.
- Put the profile field in a selection-phase `SortStepSpec(phase="selection", key_type="profile_field", field=<profile field>)`. Do NOT add a presentation-phase `policy_metric` sort step (that would reorder by the salary and change the operation's shape).
- The selection-phase sort DIRECTION follows the user's words: highest / most / largest / top => `direction="desc"`; lowest / least / smallest / bottom / fewest => `direction="asc"`.
- If the user explicitly names bachelor's/BA or master's/MA for the displayed salary metric, preserve that qualifier in `MetricSpec.degree_lane` even though the sort step is a profile field.
- Catalog resolution remains the metric authority; standalone profile rankings with no policy metric phrase are NOT this shape — they are plain profile rankings owned by ranking-and-sorting.

<examples>
<example>
User: "Show me master's starting salaries for the districts with the highest enrollment."
Plan shape: operation="rank", selection-phase sort_step profile_field=enrollment descending, primary MetricSpec(name="master's starting salary", degree_lane="ma") to display, no presentation-phase policy_metric step.
</example>
<example>
User: "What are the sick-leave days in the 10 largest districts by enrollment?"
Plan shape: operation="rank", selection-phase sort_step profile_field=enrollment descending limit=10, primary MetricSpec(name="sick leave days") to display, no presentation-phase policy_metric step.
</example>
</examples>

<counter_example>
User: "Rank districts by enrollment."
Plan shape: plain ranking owned by ranking-and-sorting — operation="rank", enrollment in profile_fields, no policy metric to display.
</counter_example>
</guidance>

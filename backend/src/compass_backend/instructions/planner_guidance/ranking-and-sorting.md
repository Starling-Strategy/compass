<guidance topic="ranking-and-sorting">
<title>Ranking and sorting</title>
<scope>Applies for ranking, sorting, ordering, top/bottom, or profile-field-ordered requests across districts.</scope>

- Separate selection from presentation: when one field chooses which districts are included and another field orders the displayed rows, use ordered `sort_steps` with a selection phase followed by a presentation phase.
- Avoid using `operation="count"` merely because the metric phrase contains "count", "number", or "counts"; use `operation="rank"` when the user asks to show a metric ordered highest, lowest, top, bottom, sorted, or first.
- When ranking by an NCES/profile field such as enrollment or FRPL share, use `operation="rank"` and put the profile-field phrase in `profile_fields` (NOT `metrics` — the catalog does not govern NCES profile fields as metrics).
- The sort DIRECTION follows the user's words. lowest / least / minimum / cheapest / bottom / fewest / smallest / ascending => `direction="asc"`; highest / most / maximum / top / largest / descending => `direction="desc"`. ALWAYS emit an explicit `SortSpec.direction` (or the selection-phase `SortStepSpec.direction`); default to `"desc"` only when no order word is present.
- Omit `LimitSpec` for unbounded requests such as "rank districts by enrollment" or "rank all covered districts"; set `LimitSpec` only for bounded sets like "top 10" or "bottom 5".
- When the user EXPLICITLY asks for every row ("rank all", "show all", "list every district", "all of them"), also set `output.row_display="all"` so the full table is shown, not a preview. A plain unbounded ranking with no "all"/"every" word ("rank districts by enrollment", "show me starting salaries") keeps the default `output.row_display="preview"` — the renderer shows a capped preview and points at the export. The distinction is the user's explicit "all"/"every", not merely the absence of a `LimitSpec`.
- When a ranking may exclude rows due to coverage, non-numeric values, or data-year gaps, still plan the ranking; execution and rendering will explain the coverage reasons.
- For "N largest districts" requests (the scope IS already enrollment-sorted), use `selection.scope="largest_districts"` with the count in `LimitSpec` and put `enrollment` in `profile_fields` (not `metrics`). Include `selection.states` for state-limited variants.
- For combined profile-sort and salary-display prompts, use a selection-phase `profile_field` sort step and omit a presentation-phase step unless the user separately requests reordering by salary.

<examples>
<example>
User: "Of the 10 largest districts by enrollment, which pay teachers the highest starting salary?"
Plan shape: operation="rank", selection-phase sort_step enrollment descending profile_field limit=10, presentation-phase sort_step starting salary descending policy_metric.
</example>
<example>
User: "Show me districts with the highest free-and-reduced lunch share and starting salary."
Plan shape: operation="rank", selection-phase sort_step FRPL descending profile_field, metric="starting salary", no presentation-phase step unless user asks to reorder by salary.
</example>
<example>
User: "Rank all covered districts by teacher workdays."
Plan shape: operation="rank", metric="teacher workdays", no LimitSpec (unbounded request), output.row_display="all" (explicit "all" => show every row, not a preview).
</example>
<example>
User: "Rank districts by starting salary, cheapest first."
Plan shape: operation="rank", metric="starting salary", sort.direction="asc" (cheapest => ascending).
</example>
<example>
User: "Which districts have the least expensive teacher salaries?"
Plan shape: operation="rank", metric="teacher salary", sort.direction="asc" (least => ascending).
</example>
<example>
User: "What are the 5 largest districts in Compass coverage by enrollment?"
Plan shape: operation="rank", selection.scope="largest_districts", profile_fields=[{name="enrollment"}], metrics=[], LimitSpec(count=5, kind="top").
</example>
</examples>
</guidance>

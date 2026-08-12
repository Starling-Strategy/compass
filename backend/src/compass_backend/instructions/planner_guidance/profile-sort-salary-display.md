<guidance topic="profile-sort-salary-display">
<title>Profile-sort salary display</title>
<scope>Applies when the user combines a teacher salary display request with a profile-field selection context such as FRPL or free-and-reduced lunch percentage.</scope>

- Keep the operation executable; use `operation="rank"` with a selection-phase `profile_field` sort step for the profile field.
- Keep the user's salary phrase as the policy metric to display; "highest" FRPL uses descending sort, "lowest" FRPL uses ascending sort.
- Skip BA-vs-MA clarification in this bounded profile-sort display context.
- If the user explicitly names bachelor's/BA or master's/MA for the displayed salary metric, preserve that qualifier in `MetricSpec.degree_lane` even though the sort step is a profile field.
- Catalog resolution remains the metric authority; standalone salary requests without the profile-field selection context may still clarify when the salary lane is ambiguous.

<examples>
<example>
User: "Show me starting teacher salaries for districts with the highest free-and-reduced lunch share."
Plan shape: operation="rank", selection-phase sort_step profile_field=FRPL descending, metric="starting teacher salary", no BA/MA clarification.
</example>
<example>
User: "Show districts with the highest FRPL share and their BA starting salary."
Plan shape: operation="rank", selection-phase sort_step profile_field=FRPL descending, MetricSpec(name="BA starting salary", degree_lane="ba").
</example>
</examples>
</guidance>

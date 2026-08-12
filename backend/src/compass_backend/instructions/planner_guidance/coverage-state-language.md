<guidance topic="coverage-state">
<title>Coverage-state language</title>
<scope>Applies when the user asks why data is missing, unavailable, not ranked, older, or only partly covered for a resolvable district and metric.</scope>

- Keep the request executable when the district and metric can be resolved; skip refusal.
- Preserve the user's district, metric, year, and coverage intent in the query plan.
- Let deterministic execution decide whether each cell is covered, issue-not-addressed, not applicable, not reviewed (including the prior-year-only sub-case), or outside the Pathfinder.
- Avoid inventing coverage wording in the plan; execution and rendering own the exact coverage explanation.

<examples>
<example>
User: "Why is Dallas ISD not ranked for this metric?"
Plan shape: operation="rank", scope="all_covered_districts", metric preserved from user phrase; execution will surface coverage state.
</example>
<example>
User: "The data for Chicago seems older — why?"
Plan shape: operation="lookup", districts=["Chicago"], metric preserved; execution explains the year-coverage state.
</example>
</examples>
</guidance>

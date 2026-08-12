<guidance topic="teacher-evaluation-observations">
<title>Teacher evaluation observation counts</title>
<scope>Applies when the user asks for observation counts, observation requirements, or the number of required observations for teacher evaluation without specifying the observation lane.</scope>

- Teacher evaluation observation metrics come in four variants: formal observations for non-tenured teachers, formal observations for tenured teachers, informal observations for non-tenured teachers, and informal observations for tenured teachers.
- **Default lane:** when the user asks for "observation counts" or "observations" without specifying formal/informal or tenured/non-tenured, default to **formal observations for non-tenured teachers** and state the assumption briefly in the response (e.g., "Showing formal observations for non-tenured teachers — let me know if you'd like the informal or tenured lane."). Do NOT clarify; answer with the default.
- Only surface the four-option clarification if the user explicitly says "all types" or signals they want a comparison across lanes.
- For ranking or sort prompts ("lowest first", "fewest", "most"), route to `operation="rank"` with the formal non-tenured observation metric and the user's sort direction.

<examples>
<example>
User: "Show me observation counts for Texas districts, lowest first."
Plan shape: operation="rank", metrics=[MetricSpec(name="formal observations for non-tenured teachers")], selection.scope="state" with states=["TX"], sort direction=asc. State the lane assumption in the response lead.
</example>
<example>
User: "Which districts require the most observations for tenured teachers?"
Plan shape: operation="rank", metrics=[MetricSpec(name="formal observations for tenured teachers")], sort direction=desc.
</example>
</examples>
</guidance>

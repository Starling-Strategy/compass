<guidance topic="policy-guidance-followups">
<title>Policy guidance follow-ups</title>
<scope>Applies when the prior turn used route="policy_guidance" and the user asks for details, sources, contracts, the top example, or to narrow the exemplars to a region.</scope>

- Retain the policy-guidance referent from the prior turn rather than reinterpreting the topic from scratch.
- Avoid asking which metric family the user means merely because an exemplar topic overlaps policy metrics.
- When the approved guidance library can satisfy the follow-up, stay on route="policy_guidance"; otherwise emit a typed executable plan with the district and governed policy-detail phrase already visible in the prior context.
- A regional refinement ("just districts in the South", "the Midwestern ones", "narrow to the Northeast", "out West") stays on route="policy_guidance": keep the prior turn's topic_ids, layers, and focus_terms, and set `region` to the region phrase the user named ("the South", "the Midwest", "the Northeast", "the West"). All four U.S. Census regions are governed. Do not enumerate states and do not re-prompt for the topic — the renderer expands the governed region and filters the exemplars.
- Planner guidance is not policy content — the policy-guidance library and renderer own the user-facing stance, rationale, exemplar, source, and citation language.

<examples>
<example>
User: "Can you give me the details for the top example?" (after a policy_guidance exemplar response)
Plan shape: route="policy_guidance", same topic/layer/focus context from prior turn, response_mode="exemplar_detail" with selected_exemplar_ids from prior context.
</example>
<example>
User: "Narrow that to just districts in the South." (after a policy_guidance exemplar response)
Plan shape: route="policy_guidance", same topic_ids/layers/focus_terms from prior turn, region="the South". No state enumeration, no topic re-prompt.
</example>
<example>
User: "What's the actual contract language for that district?" (after a policy_guidance exemplar)
Plan shape: route="execute", district from prior exemplar, governed policy-detail metric phrase from prior context.
</example>
</examples>
</guidance>

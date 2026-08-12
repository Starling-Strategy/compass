<guidance topic="policy-guidance-advisory-followup">
<title>Policy-guidance advisory follow-ups</title>
<scope>Applies when the prior turn used route="policy_guidance" and the user asks an advisory or comparative judgement question that weighs two policy priorities against each other — "should we prioritize improving pay or improving benefits?", "is it better to invest in X or Y?", "which matters more, X or Y?".</scope>

- This is NOT a request to re-list exemplars for each topic. Do not emit a multi-topic `layers=["exemplars"]` plan — that produces parallel exemplar bundles that ignore the user's "which should we prioritize" framing and read as two separate answers stapled together.
- Stay on `route="policy_guidance"` and answer the prioritization question itself from NCTQ's published positions and research. Set `layers=["stances","rationales"]` so the renderer leads with NCTQ's stance on each priority and the research that explains how they relate, not with curated district picks.
- Set `response_mode="advisory_comparison"`. This is the signal the renderer reads to frame the answer as a weigh-these-priorities decision (a "not strictly either/or" lead-in and closing) instead of two stacked topic dumps. Without it the renderer falls back to the generic "Here is NCTQ policy guidance for X and Y" framing that reads as two separate answers.
- Carry both topics the user is weighing in `topic_ids` (keep the prior turn's topic plus the one the comparison introduces — e.g. the prior `benefits` turn plus `general-salary` when the user adds pay). Set `primary_topic_id` to the topic the prior turn established so the renderer keeps continuity.
- The answer is a synthesis, not a binary verdict: NCTQ does not hold a single either/or stance ranking pay above benefits or vice versa. The renderer owns the stance/rationale language — your job is to route both topics' stances and rationales into one advisory answer rather than two exemplar dumps.

<examples>
<example>
User: "Should we prioritize improving pay or improving benefits?" (after a benefits exemplar response)
Plan shape: route="policy_guidance", topic_ids=["benefits","general-salary"], layers=["stances","rationales"], primary_topic_id="benefits", response_mode="advisory_comparison". No exemplars layer, no per-topic exemplar dump.
</example>
</examples>
</guidance>

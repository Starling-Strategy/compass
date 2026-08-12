<guidance topic="peer-salary-comparison">
<title>Peer salary comparison</title>
<scope>Applies when the user asks for maximum teacher salary across peer, comparable, or similar districts relative to one anchor district.</scope>

- Use `operation="peer_comparison"` for anchor-plus-peer salary requests; preserve the anchor district and the user's maximum-teacher-salary phrase.
- Let governed catalog aliases decide the salary metric; the planner should not invent a metric ID or narrow the salary lane.
- For requests that are not peer/comparable-district comparisons, leave broad salary ambiguity to the normal catalog clarification path.
- Set `peer_overrides` only when the user explicitly requests a weighting bias or state exclusion; omit it entirely when no such preference is stated.
- Choose `feature_set` based on the user's emphasis: `"enrollment"` for size/enrollment weighting, `"frpl"` for poverty-level or demographic weighting, `"locale"` for urban/rural weighting, `"all"` when no specific factor is mentioned.
- Set `exclude_states` to a list of two-letter abbreviations when the user asks to exclude a state from the peer candidate pool; this is a pre-scoring exclusion, not a post-scoring filter.

<examples>
<example>
User: "Compare Denver Public Schools and its peer districts on maximum teacher salary, weighted by FRPL share."
Plan shape: operation="peer_comparison", metrics=[{name="maximum teacher salary"}], peer_overrides={feature_set="frpl"}.
</example>
<example>
User: "Compare Denver's salary to comparable districts outside California."
Plan shape: operation="peer_comparison", metrics=[{name="maximum teacher salary"}], peer_overrides={exclude_states=["CA"]}.
</example>
<example>
User: "Compare Denver and its peers on maximum teacher salary." (no weighting mentioned)
Plan shape: operation="peer_comparison", metrics=[{name="maximum teacher salary"}] — no peer_overrides field.
</example>
</examples>
</guidance>

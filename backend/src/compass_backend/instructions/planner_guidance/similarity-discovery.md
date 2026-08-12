<guidance topic="similarity-discovery">
<title>Similarity / peer-set discovery</title>
<scope>Applies when the user asks who the peer districts are for a named anchor district, with or without a policy metric comparison.</scope>

- Use `operation="similarity"` when the user asks only who the peers are, with no policy metric mentioned.
- Use `operation="peer_comparison"` when the user asks both who the peers are AND wants to compare a policy metric across them.
- If the prompt mentions any policy metric phrase (salary, salaries, pay, workdays, sick leave policies, leave days, hours, premiums, observation count, etc.) alongside peer/comparable/similar phrases, route to `operation="peer_comparison"` with that metric in `metrics`.
- For `operation="similarity"`, always set `anchor_name` (the named anchor district string), `feature_set`, `exclude_states`, and `limit`; set `selection.scope="named_districts"` with `selection.districts=[anchor_name]`.
- For `operation="peer_comparison"`, do NOT set the `similarity` field, and do NOT carry `anchor_name`/`feature_set`/`exclude_states` as a similarity payload — leave `similarity` absent. The anchor comes from `selection.districts[0]` (set `selection.scope="named_districts"` with `selection.districts=[anchor_name]`), and any optional peer-scoring biases go in `peer_overrides` (its `feature_set`/`exclude_states`), never in a similarity payload. A `peer_comparison` plan that carries a `similarity` payload is rejected by the contract validator.
- Choose `feature_set` from: `"enrollment"` (size/enrollment emphasis), `"frpl"` (poverty level or demographic emphasis), `"locale"` (urban/rural emphasis), `"all"` (default, no specific factor mentioned).
- Omit `metrics` entirely for `operation="similarity"` — discovery returns the peer set only.

**Uncovered anchor district.** When the named anchor is NOT in the covered Pathfinder universe, do NOT ask the user to name replacement districts — proceed with the similarity or peer_comparison route anyway, using the uncovered district name as the anchor. The executor resolves covered peers via geographic and demographic proximity. Briefly acknowledge in the response that the named district is not in the Pathfinder and that you are comparing with nearby covered districts instead. Example: "Fresno Unified is not currently in the District Policy Pathfinder, so I compared similar covered California districts on sick leave." Never say "Compass doesn't have a geographic proximity feature" — it does; the similarity execution finds covered peers automatically.

<examples>
<example>
User: "Find peers to Denver."
Plan shape: operation="similarity", anchor_name="Denver", feature_set="all", exclude_states=[].
</example>
<example>
User: "Find peers to Denver and show their salaries."
Plan shape: operation="peer_comparison", selection.districts=["Denver"], metrics=[{name="salaries"}].
</example>
<example>
User: "How does Denver's teacher salary compare to nearby districts?"
Plan shape: operation="peer_comparison", selection.districts=["Denver"], metrics=[{name="teacher salary"}]. No `similarity` field.
</example>
<example>
User: "Comparable enrollment districts outside California for Denver."
Plan shape: operation="similarity", anchor_name="Denver", feature_set="enrollment", exclude_states=["CA"].
</example>
<example>
User: "Who are Denver's peer districts and how do sick leave policies compare?"
Plan shape: operation="peer_comparison", selection.districts=["Denver"], metrics=[{name="sick leave policy"}].
</example>
</examples>
</guidance>

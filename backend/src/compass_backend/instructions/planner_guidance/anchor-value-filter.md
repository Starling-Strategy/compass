<guidance topic="anchor-value-filter">
<title>Same-value-as-anchor equality filter</title>
<scope>Applies when the user names one anchor district and asks which other districts share that district's value for a metric — "the same X as [anchor]", "the same X as me", "matches our X", "as many ... as [anchor] has". This is an equality filter on a metric value, NOT a peer/similarity request.</scope>

- This is a `FilterSpec.anchor_value` equality filter, not `operation="peer_comparison"` and not `operation="similarity"`. The anchor only supplies the value to match; the user wants every covered district whose value equals it.
- Routing test: "same VALUE for a metric as [anchor]" → anchor-value filter. "peer / comparable / similar districts to [anchor]" (who is like the anchor by characteristics) → `similarity` or `peer_comparison` per `similarity-discovery.md`. The word "same" plus a named metric is the equality signal; "peer/comparable/similar" is the characteristics signal. They are different operations — do not route an equality ask to peer_comparison.
- Shape: `route="execute"`, `operation="lookup"` (or `operation="count"` when the user only asks how many match), `selection.scope="all_covered_districts"`. Put the metric phrase in `metrics` and mirror it in the filter's `field`. Add `filters=[FilterSpec(field="<metric phrase>", operator="equals", anchor_value={"district":"<anchor>"}, kind="metric_value")]`. Keep the anchor in `anchor_value`, never in `selection.districts` (that would scope the answer to the anchor itself).
- The executor looks up the anchor's value, then returns every covered district that equals it and excludes the anchor row. Report the matching list with a count and the covered denominator.
- If the metric phrase is ambiguous (e.g. "school year length" → teacher workdays vs. student-contact days), clarify the metric first; once the user picks, emit the anchor-value filter shape above. Do not switch to peer_comparison after clarification.
- When you clarify the metric, the clarification's `pending_context` keeps the SAME anchor-value shape: `selection.scope="all_covered_districts"` with `selection.districts=[]` (empty), and the anchor district in a `FilterSpec.anchor_value`. Never put the anchor in `pending_context.selection.districts` — `all_covered_districts` scope must not set districts, so leaking the anchor there fails plan validation and discards the clarification.

<examples>
<example>
User: "I'm in Portland ME. What other districts have the same length school year?" (after the user picks "middle school student-teacher contact days")
Plan shape: operation="lookup", selection.scope="all_covered_districts", metrics=[{name="Contracted student-teacher contact days per academic year in middle school"}], filters=[{field="Contracted student-teacher contact days per academic year in middle school", operator="equals", anchor_value={"district":"Portland ME"}, kind="metric_value"}].
</example>
<example>
User: "Which districts have the same starting salary as Dallas ISD?"
Plan shape: operation="lookup", selection.scope="all_covered_districts", metrics=[{name="starting salary"}], filters=[{field="starting salary", operator="equals", anchor_value={"district":"Dallas ISD"}, kind="metric_value"}].
</example>
<example>
User: "How many districts give teachers the same number of sick days as us in Denver?"
Plan shape: operation="count", selection.scope="all_covered_districts", metrics=[{name="sick days"}], filters=[{field="sick days", operator="equals", anchor_value={"district":"Denver"}, kind="metric_value"}].
</example>
</examples>
</guidance>

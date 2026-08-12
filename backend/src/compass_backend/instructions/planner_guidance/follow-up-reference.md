<guidance topic="follow-up-reference">
<title>Follow-up reference</title>
<scope>Applies when the user refers to prior results using phrases like "those districts", "the N districts", "those N", "name them", "list them out", or "the ones from before".</scope>

- When the user means the exact rows already shown, set `inherit_selection_from="prior_result_rows"` on `QueryPlan`; the downstream resolver materialises the district list from `QueryContext.result_districts` (including `qualifying_district_ids` from a prior count).
- **Population vs. matches after a count (case 393).** A prior count states two numbers — the matched subset (e.g. "19 match") and the population it counted over (e.g. "of 90 covered districts with data"). When the follow-up asks for the *population* — *"who are the 90?"*, *"which districts have data on this?"*, *"who is in the denominator?"* — present the population, not the matches: set `inherit_selection_from="prior_result_population"`. The downstream merge layer then materialises the population from `QueryContext.result_population_districts` and drops the count's value threshold so the list is not re-narrowed (these invariants apply wherever this flag is set — you do not need to clear the filter yourself). For the strongest cases the deterministic detector forces this flag for you (it fires when the referent's N equals the prior count's denominator — e.g. *"who are the 90?"*); on phrasings it does not recognise (e.g. *"which districts have data on this?"*), emit the flag yourself. When the follow-up names the *matched* count — *"what are the 19 districts?"* — keep `prior_result_rows`.
- Alternatively, set `selection.scope="named_districts"` and `selection.districts` to a sentinel string the resolver recognises; the authoritative set is `_SELECTION_REFERENT_SENTINELS` in `planning/planner.py` — currently `"<inherit>"`, `"inherit"`, `"those"`, `"those districts"`, `"these"`, `"these districts"`, `"that"`, `"that list"`, `"the previous"`, `"previous districts"`, `"prior districts"`, or `"prior result"`. The resolver replaces the sentinel with the concrete prior districts.
- Add any new metric, sort, or filter the user requests on top of the inherited set rather than replacing the selection.
- When the user wants the same original scope in a different display shape (export, chart, different sort), set `inherit_selection_from="prior_query"` instead.
- If the prior turn was itself a clarification rather than a resolved result, ask one targeted clarification rather than inventing a referent.

<hard_rules>
**Forbidden — list-those-N refusal.** When the user uses *"the N districts"*, *"those N"*, or *"those N districts"* — where N is the exact count from the prior turn — do NOT return *"I could not structure that request safely"* and do NOT present a fresh metric clarification menu. The deterministic delta-intent detector fires on this shape; the DELTA-INTENT CONSTRAINT block in your system prompt is binding. The only acceptable alternative to inheriting `prior_result_rows` is `route=clarify` with a specific reason (e.g., the newly named metric is not in the catalog) — never a generic refusal.

Canonical prompts this rule covers (each is a turn-2 / turn-3 list-those-N follow-up after a prior count or list turn):

- *"What are the 17 districts and how many observations do they require?"*
- *"What are those districts and how many observations does each require?"*
- *"What is the minimum performance pay for those districts?"*

For all of these, emit `route=execute` with `inherit_from_session=true`, `inherit_selection_from="prior_result_rows"`, `selection.scope="unspecified"`, and the new metric on top.
</hard_rules>

<examples>
<example>
User: "What are the 17 districts and how many observations do they require?" (after turn 1 counted 17 districts meeting a threshold)
Plan shape: operation="lookup", inherit_from_session=true, inherit_selection_from="prior_result_rows", new metric added on top of inherited set.
</example>
<example>
User: "Export that as a CSV."
Plan shape: inherit_from_session=true, inherit_selection_from="prior_query", OutputSpec.format="csv_export".
</example>
<example>
User: "Sort those by ending salary instead."
Plan shape: inherit_from_session=true, inherit_selection_from="prior_result_rows", selection.scope="unspecified", metrics=[], sort field updated.
</example>
</examples>
</guidance>

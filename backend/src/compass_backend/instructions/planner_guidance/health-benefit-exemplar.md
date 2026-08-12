## Health-benefit exemplar request

When the user asks for **districts that have great / good / strong / best / model / exemplary** teacher health coverage, health benefits, or health insurance, they want NCTQ-curated policy exemplars — not a ranking, count, or comparison.

Emit:

- `route="policy_guidance"`
- `policy_guidance.topic_ids=["benefits"]`
- `policy_guidance.layers=["exemplars"]`
- `policy_guidance.focus_terms=["health benefits"]`
- `policy_guidance.intent_summary` describing the user's ask (e.g. "User asked for NCTQ-curated examples of districts with strong teacher health coverage.")

### When to **clarify or execute instead**

Do NOT emit `policy_guidance` if the user message is data-shaped:

- Asks "how many", "count", or "distribution"
- Uses ranking verbs ("top 10", "ten districts", "most", "least", "rank")
- Names a specific metric ("100% of premium", "premium share", "specific metric", "each type")

These are data questions about benefits, not exemplar requests. Route them through normal selection or clarify if ambiguous.

### Example

- "Show me districts where teachers get great health coverage." → `policy_guidance`, topic_ids=["benefits"], layers=["exemplars"]
- "Which districts cover 100% of the employee health insurance premium?" → execute / clarify (data question, not exemplar)
- "Rank districts by benefits." → execute / clarify (ranking, not exemplar)

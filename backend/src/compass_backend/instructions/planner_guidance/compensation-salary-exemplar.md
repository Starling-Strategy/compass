## Compensation / teacher-pay exemplar request

When the user asks — as a **bare subjective superlative** — who has the **best / strongest / leading / "good money"** teacher pay, salaries, or compensation, they are asking a "who does this well" question. "Best" pay is subjective, and the right response is NCTQ's curated compensation exemplars framed across multiple compensation lenses — **not** a single raw salary ranking, and **not** a clarification asking which salary metric to use.

Emit:

- `route="policy_guidance"`
- `policy_guidance.topic_ids=["general-salary"]`
- `policy_guidance.layers=["exemplars"]`
- Leave `policy_guidance.focus_terms` empty — a bare "best pay" superlative is not focused on one narrow sub-aspect, and a focus term narrows the exemplar set to a single district. The user wants the full set of curated salary exemplars across multiple lenses.
- `policy_guidance.intent_summary` describing the ask (e.g. "User asked, subjectively, who has the best teacher pay; wants NCTQ-curated compensation exemplars across multiple lenses.")

The exemplar answer acknowledges that "best" is subjective and frames compensation across meaningful dimensions (e.g. strong starting salary, fast early-career growth, performance-linked pay) rather than dumping only the highest raw salaries. **Do NOT ask the user which salary metric they mean** — the subjective superlative is the signal to give the framed exemplar answer.

Treat **"who pays the most", "pay teachers the most", "highest-paying", "doing the best job on pay"** as the *same* subjective superlative as "best pay" — a bare superlative with no explicit ranking structure (no "top N", no named metric) is an exemplar request, **not** a ranking. Route it to exemplars; do **not** clarify which salary metric. Only a request with explicit ranking structure ("top 10", "rank …", "highest first") or a named metric is a ranking that should execute.

### When to **clarify or execute instead**

Do NOT emit `policy_guidance` when the message is data-shaped or names a concrete target:

- Explicit ranking structure: "top 10", "ten districts", "rank", "ranked", "highest first", "lowest first", "list the districts"
- A concrete metric, lane, or threshold: "BA starting salary", "master's salary", "maximum salary", "more than $X", "above $50k"
- "how many", "count", or a distribution
- A named district lookup ("what is starting pay in Dallas ISD")

These are data questions — route them through normal selection/ranking or clarify only if genuinely ambiguous. The exemplar route is for the **bare superlative** ("who has the best pay?"), not for a ranking or a specific-metric request.

### Examples

- "Who has the best teacher pay?" → `policy_guidance`, topic_ids=["general-salary"], layers=["exemplars"], focus_terms=[] (empty — surface the full set of salary exemplars).
- "Who's leading the way on teacher salaries?" → `policy_guidance`, general-salary exemplars (subjective superlative, no metric named).
- "Where should I teach if I want to make good money?" → `policy_guidance`, general-salary exemplars (frame the compensation lenses; do not clarify the metric).
- "Show the top 10 districts by BA starting salary." → execute (explicit ranking + concrete lane), NOT exemplars.
- "What is starting pay for Dallas ISD?" → execute (named-district lookup), NOT exemplars.

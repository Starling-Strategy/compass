## Differentiated pay inventory (unspecified type)

"Differentiated pay" is a *family* of policies, not one metric — it spans performance pay, hard-to-staff **school** stipends, hard-to-staff **subject** stipends, and other differentiated pay (National Board, mentoring, leadership/coaching stipends). When the user asks whether districts offer "differentiated pay" **without naming a specific type** — e.g. *"Do any of the districts in the South offer differentiated pay?"* — answer it as an **executable inventory across the family. Do not clarify or refuse**; commit to one useful view and offer the narrower cuts.

Emit:

- `route="execute"`
- `query_plan.operation="lookup"`
- **Geography** — scope to whatever the user named, and let deterministic resolution expand it:
  - A governed region (e.g. *"the South"*, *"Southern"*): `query_plan.selection.scope="state"`, `query_plan.selection.states=["the South"]` — pass the region phrase through; the executor expands it to the governed member states. Do **not** enumerate the states yourself.
  - One or more named states: `selection.scope="state"`, `selection.states=[...]`.
  - Named districts: `selection.scope="named_districts"`, `selection.districts=[...]`.
- `query_plan.metrics` — one "does the district offer …" anchor per differentiated-pay subtopic (use these exact names; first `primary`, the rest `comparison`):
  - `MetricSpec(name="Performance pay based on evaluation ratings, unrelated to salary schedule", role="primary")`
  - `MetricSpec(name="District offers additional pay for teaching in schools classified as hard-to-staff", role="comparison")`
  - `MetricSpec(name="District offers additional pay for teaching subjects deemed \"hard to staff\"", role="comparison")`
  - `MetricSpec(name="Additional pay for National Board Certification", role="comparison")`
- `query_plan.limit.kind="all"`
- `query_plan.output.format="table"`

Use the metric names exactly as written — catalog resolution depends on them.

### Offer the narrower cuts

This inventory is **one way to look at differentiated pay**. In the answer, note that the user can narrow to a specific lens if they want — frame it as *"here's one way to look at it; I can break out any of these in more detail"*, never a refusal or a bare "which did you mean?":

- **Performance pay** — bonuses tied to evaluation ratings
- **Hard-to-staff schools** — extra pay for teaching in high-need schools
- **Hard-to-staff subjects** — extra pay for shortage subjects (special education, English learners, math, science)
- **Other differentiated pay** — National Board certification, mentoring, leadership/coaching stipends

### Required signals

- A *"differentiated pay"* phrase, with **no** specific subtopic named. If the user already names a specific type (e.g. *"performance pay"*, *"hard-to-staff"*, *"National Board"*), use normal catalog metric recognition for that metric instead of the family inventory.

### Counter-examples

- "What's the maximum performance pay bonus in Dallas?" → a specific metric is named; use normal recognition, not the family inventory.
- "What data do you have on differentiated pay?" → a capability / data-inventory request (`route="direct"`), handled by the data-inventory guidance.
- "How many districts offer differentiated pay?" → a `count`, not an inventory.

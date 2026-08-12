<guidance topic="teacher-compensation-salary">
<title>Teacher compensation salary</title>
<scope>Applies for any request involving teacher salary, degree-lane qualification, compensation thresholds, vague-magnitude bonus phrases, or multi-lane ranking prompts that combine BA and MA salary.</scope>

- Preserve the user's salary wording and let catalog resolution decide whether Compass needs BA starting salary, master's salary, maximum salary, average salary, or another approved salary metric.
- A salary request is a `MetricSpec`, never a `profile_field`. `teachers_fte` (and other headcount/profile fields) is a teacher *headcount*, not pay — never route "salary", "starting salary", "pay", or "compensation" to it.
- A non-inherited salary `lookup` must carry at least one salary `MetricSpec`. Never emit `metrics=[]` with only a `profile_field` — that drops the salary the user asked for and fails plan validation (`non-inherited query plans require at least one metric`).
- For salary `MetricSpec`s, leave `degree_lane=null` when the user does not name a lane — do not silently default to BA. The catalog applies the reviewed BA default with a renderer disclosure when the lane is null. Only set `degree_lane="ba"` or `degree_lane="ma"` when the user explicitly names bachelor's/BA or master's/MA.
- Keep the user's descriptive prose in `MetricSpec.name` — and when the user names a degree lane, keep the lane word in that prose too (e.g. `"BA starting salary"`, `"master's starting salary"`), never reducing a lane-qualified request to a bare `"starting salary"`. Mirror that lane-bearing phrase onto any `sort.field` / `FilterSpec.field`, and still set `degree_lane` alongside it. Treat bachelor's/BA and master's/MA the same way — do not drop the BA lane just because it is the catalog default.
- Leave `degree_lane=null` for non-salary metrics such as workdays or observations where lane is meaningless.
- For lane-qualified threshold filters, mirror the same prose in both `MetricSpec.name` and `FilterSpec.field` so the executor can inherit the degree lane from the MetricSpec; mismatched prose between name and field breaks lane inheritance.
- For ranking/top/highest prompts that ask for both BA and MA starting salary together, treat as supported multi-metric — emit `operation="rank"` with one `MetricSpec` per lane and `requires_composite_ranking=True` unless the user explicitly asks for one side-by-side table. Do NOT return generic unsupported-shape prose, and do NOT tell the user to run one metric first.
- For peer-comparison prompts asking for maximum teacher salary, use `operation="peer_comparison"` with the anchor district and the user's broad phrase; governed catalog aliases handle package-specific salary authority. Standalone maximum-salary prompts may still clarify when no peer/comparable-district context is present.
- For vague-magnitude compensation phrases ("real bonuses", "meaningful bonuses", "substantial pay"), pick a defensible numeric threshold AND set `FilterSpec.threshold_hint` to the user's phrase; omit `threshold_hint` when the user gives a concrete dollar amount.

<examples>
<example>
User: "What is the starting salary for teachers in Wake County?"
Plan shape: operation="lookup", selection.scope="named_districts", districts=["Wake County"], metrics=[MetricSpec(name="starting salary", degree_lane=null)]. Route "starting salary" to a salary MetricSpec — never to teachers_fte or any profile field, and never leave metrics empty.
</example>
<example>
User: "Compare starting salaries in District A and B."
Plan shape: operation="lookup", MetricSpec(name="starting salaries", degree_lane=null). No lane named, so leave the lane null; the catalog applies the reviewed BA default with a renderer disclosure.
</example>
<example>
User: "Compare starting salaries for teachers with a master's degree."
Plan shape: operation="lookup", MetricSpec(name="starting salaries for teachers with a master's degree", degree_lane="ma").
</example>
<example>
User: "What is the highest starting salary for a teacher with a BA degree?"
Plan shape: operation="rank", selection.scope="all_covered_districts", limit=1, metrics=[MetricSpec(name="BA starting salary", degree_lane="ba")], sort=SortSpec(field="BA starting salary", direction="desc"). The user named the BA lane, so keep "BA" in the metric name AND on sort.field — do NOT draft a bare "starting salary". This mirrors the master's phrasing above; the BA lane is preserved even though it is the catalog default.
</example>
<example>
User: "Districts where master's starting salary > $50K."
Plan shape: MetricSpec(name="master's starting salary", degree_lane="ma"), FilterSpec(field="master's starting salary", operator="greater_than", value=50000, kind="metric_value").
</example>
<example>
User: "Starting salary for teachers with a BA and teachers with an MA" (after a top-10 / highest-teacher-salaries context).
Plan shape: operation="rank", selection.scope="all_covered_districts", limit=10, metrics=[MetricSpec(name="BA starting salary", degree_lane="ba"), MetricSpec(name="MA starting salary", degree_lane="ma")], requires_composite_ranking=True.
</example>
<example>
User: "Districts with meaningful hard-to-staff bonuses."
Plan shape: MetricSpec(name="hard-to-staff school pay"), FilterSpec(field="hard-to-staff school pay", operator="greater_than_or_equal", value=1000, kind="metric_value", threshold_hint="meaningful").
</example>
</examples>
</guidance>

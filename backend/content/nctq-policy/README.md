# NCTQ Policy Content Markdown Stopgap

This directory is a lightweight, Git-managed stopgap for NCTQ policy content.
It was generated from reviewed source material so Compass can serve NCTQ's
positions, research rationales, and exemplary policies while the fuller content
migration is completed.

Generated from the staging database on 2026-05-07:

- `compass.nctq_rationales`
- `compass.nctq_exemplar_policies`
- `compass.topic_aliases`
- `compass.nctq_topic_briefs`

Reviewed against the three Google Drive source documents on 2026-05-07:

- District Policy Stances (NCTQ internal Google Doc)
- Research rationales (NCTQ internal Google Doc)
- Exemplary Policies (NCTQ internal Google Doc)

This content is already wired into the Compass runtime. The backend loads it at
startup through a strict, deterministic parser; invalid content stops the app
from starting instead of producing guidance with broken provenance. The live
flow is:

```text
Markdown files -> deterministic parser -> typed policy content library -> policy-guidance renderer -> structured citations
```

## Content Layers

- `stances`: short NCTQ policy positions.
- `research_rationales`: NCTQ-authored rationale text that explains why a stance exists.
- `exemplary_policies`: named district examples with source URLs.
- `topic_brief`: current topic-page metadata where it exists.

## Citation Status

Every rationale and exemplar must have a `source_url` and a `citation_status` of either `ready` or `placeholder`. The historical `needs_source_url` value has been retired and is rejected by the strict loader (`src/compass_backend/policy_guidance/loader.py`).

- **`ready`** — `source_url` is a real public NCTQ URL (e.g. `https://teacherquality.nctq.org/contract-database/district/<slug>` for exemplars).
- **`placeholder`** — `source_url` is exactly the Pathfinder homepage `https://www.nctq.org/district-policy-pathfinder/`. Used while topic-anchored Pathfinder pages are still being authored. Users still get a clickable, NCTQ-owned destination.

A Pydantic `model_validator` enforces the two states symmetrically: a `placeholder` rationale must use the homepage URL, and a `ready` rationale must NOT use it. That symmetry catches drift in both directions — you can't accidentally mark something `ready` while leaving the homepage URL, and you can't fill in a real URL while leaving the status at `placeholder`.

When NCTQ launches topic-anchored Pathfinder pages, replace each placeholder
rationale's `source_url` with the topic/section URL and flip
`citation_status: placeholder → ready`. Add or update the topic-level
`canonical_url:` frontmatter for that topic at the same time. Redirect-only
topics do not need their own canonical URL. This cleanup is tracked under
SSN-254.

Until those URLs land, the runtime renderer (`src/compass_backend/rendering/policy_guidance.py`) prefixes any rendered bullet whose citation is `placeholder` with the visible marker `[provisional source]`. This signals to readers that the link is a stopgap to the Pathfinder homepage rather than a real per-topic source page. Flipping `citation_status` to `ready` removes the marker automatically — no renderer change required.

## What Remains

The research-rationale Google Doc is substantially richer than the compact
DB-derived rationale summaries in these topic files. Do not treat the rationale
layer as fully migrated until the full source document has been split into
topic/subtopic sections with source references.

The next step is to make that migration without losing provenance: split the
source material into structured topic and subtopic entries, preserve its source
references, and validate the changed library before release. Until then, this
repository is the reviewed runtime snapshot—not a claim that every research
rationale from the source document has been migrated.

## Runtime Contract

Compass should not hand the whole folder to the model. It should resolve topic and user intent first, then inject only the relevant layer:

- "What does NCTQ believe?" -> stances.
- "What does the research say?" -> research rationales.
- "Show me a district that does this well" -> exemplary policies.
- "Give me the full picture" -> stances, rationales, and exemplars.

Stable IDs in headings are part of the contract and should not be changed casually.

# Metric Calculator (adjacent system, not part of Compass)

A reader of this repository will run into "Metric Calculator" in a few places —
schema comments, a `source = 'piedpiper'` filter, a dashboard nav item — and
reasonably wonder if it's a Compass component. It isn't. This page is the
container for that context: what Metric Calculator is, how it relates to the
data Compass reads, and why its code lives outside this repo's scope.

## The one-line version

**Metric Calculator is NCTQ's internal, human-in-the-loop tool for turning raw
district policy documents into reviewed data.** It is upstream of Compass, not
part of it: Compass never calls its code, and reads none of its outputs
directly. The connection is indirect, through NCTQ's published policy
database — see [How it reaches Compass](#how-it-reaches-compass) below.

## Major concepts

| Term | What it is |
| --- | --- |
| **PiedPiper** | The prediction engine. Given a district's source documents (contracts, handbooks, board policies) and a catalog question ("what is the minimum starting teacher salary?"), it runs several independent AI passes at varying retrieval depth, votes across them, and produces a suggested answer with supporting quotes, a confidence score, and a vote-agreement score. It never publishes on its own. |
| **Metric Calculator** | The analyst review application (an internal dashboard at nctq.ai). It shows a human reviewer PiedPiper's suggested answer, its citations, and its voting statistics, and lets them approve, reject, or hold it. Nothing PiedPiper produces becomes official NCTQ data without this step. |
| **TCD / District Policy Pathfinder** | NCTQ's published policy database and its public product name. Once an analyst approves an answer in Metric Calculator, it's published into this system. The code in this repository calls it "TCD" (the name baked into schema and sync-job code); NCTQ's public site calls the same underlying dataset the **District Policy Pathfinder**. |
| **bronze / silver / gold** | The same three-stage data-cleaning pattern Compass's own Databricks pipeline uses (see [§3](../03-data-and-databricks.md#the-nightly-pipeline)), but a separate instance of it — Metric Calculator's staging tables, not Compass's. Sharing the pattern is a coincidence of two teams solving the same kind of problem, not a shared pipeline. |

## Timeline

| When | What happened |
| --- | --- |
| Earlier attempt | A full-stack rebuild (Next.js + FastAPI + a PydanticAI/LlamaIndex "engine") attempted the same idea end to end: index documents, retrieve and validate citations, calculate a metric. It's archived; none of its code was carried forward. |
| Next iteration | The idea was rebuilt more simply as a batch pipeline nicknamed PiedPiper: chunk and index documents, run many independent AI predictions per question, vote, validate citations, write a suggested answer. This is the version that actually produced data. |
| Folded into one codebase | PiedPiper and the analyst-review dashboard were consolidated into what's now `policy-advisor`, Starling's monorepo for NCTQ work (the repo Compass itself was carved out of). |
| Archived, kept live | PiedPiper's generation code was later archived (removed from the active tree, kept in git history) as out of scope for the Compass v1 push — it had no callers left in the active codebase and no test coverage. The **review dashboard stayed live**: analysts still use it today to work through PiedPiper's historical output. Starting a brand-new PiedPiper run currently doesn't work (a known, tracked bug — the "run predictions" button calls code that no longer exists); reviewing and approving already-generated answers still does. |

## How it reaches Compass

The path is real but indirect — three hops, not a shared pipeline:

```mermaid
flowchart LR
    DOCS["District policy documents"] --> PP["PiedPiper (prediction engine)"]
    PP --> MC["Metric Calculator (analyst review, nctq.ai)"]
    MC -->|"approved answers"| TCD["TCD / District Policy Pathfinder (NCTQ's published database)"]
    TCD -->|"nightly Databricks sync"| CS[("compass schema: navigator_* tables")]
    CS --> COMPASS["Compass chat"]
```

Compass's nightly sync (`sync_navigator.py`, see
[§3 Where the data comes from](../03-data-and-databricks.md#where-the-data-comes-from))
pulls from the TCD/Pathfinder database the same way it would from any other
third-party source — Compass has no awareness that Metric Calculator or
PiedPiper exists upstream of it. If Metric Calculator's review queue stalls or
an approval is wrong, that surfaces in Compass only as stale or incorrect
`navigator_*` data, days later, through the normal sync — not as a direct
failure anyone would trace back to "Metric Calculator" without knowing this
chain.

## What this means for Compass

- Compass's own grounding rules ([§2](../02-product-and-answer-flow.md)) — typed
  plans, catalog resolution, citations attached at answer time — are unrelated
  to how PiedPiper grounds its predictions. Don't assume PiedPiper's citation
  logic when reasoning about Compass's, or vice versa; they were built years
  apart by different validation logic.
- A bug in a Compass answer's underlying value is very unlikely to be fixable
  in this repo if the root cause is upstream in TCD/Pathfinder data — that
  requires an NCTQ Metric Calculator review, not a Compass code change.
- Full technical detail on PiedPiper and Metric Calculator (pipeline mechanics,
  code history, the archived-but-recoverable source) lives in the
  `policy-advisor` repo's legacy notes, not here — this page is deliberately
  just enough to keep this repo's readers oriented.

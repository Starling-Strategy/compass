# 2. Product & Answer Flow

**How Compass turns a question into a grounded, cited answer.**

## The short version

When someone asks Compass a question (*"What's the starting teacher salary in
Denver?"*), the data in the answer is not something an AI model made up. Compass
works in separated stages: an AI model **plans** what the question is asking for; a
deterministic layer **verifies** that every district, topic, and metric in that plan
actually exists in NCTQ's catalog; plain database queries **fetch** the facts; a
renderer **assembles** the answer with its tables and citations; and, when a
response qualifies for polish, an AI model **phrases** the result in plain language,
under validation rules that reject a rewrite that adds or changes a number. The
facts and the wording come from different places, on purpose.

Two properties fall out of this design:

- **Every value traces to a real database row.** Models never supply facts, IDs, or
  citations; they supply intent and phrasing.
- **Every answer shows its work.** Data cells carry citation markers that resolve to
  the actual source documents NCTQ reviewed.

The rest of this doc walks the pipeline in order, then covers the prompt
architecture and the voice standards. It assumes technical fluency from here on.

## The pipeline

Read the diagram top to bottom. The central path is a data question; the short
branches show what happens when Compass instead needs clarification, can reply
directly, or retrieves approved NCTQ material. Color marks the division of labor:
blue plans, purple writes, green makes small bounded judgments, and gray boxes are
plain code. No AI box is ever the source of a fact. The diagram names roles rather
than models because the model for each role is a configuration setting, explained
in [How Compass uses different AI models](#how-compass-uses-different-ai-models).

```mermaid
flowchart TD
    U([A user asks a question]) --> FE[Chat window]
    FE --> API[Compass API]

    subgraph API_TURN ["One turn inside Compass"]
        M["Load earlier context<br/>Plain code"] --> P["1. Plan<br/>The planning model creates a typed plan<br/>and chooses what happens next."]
        P --> ROUTE{Which route?}

        ROUTE -->|Data question| R["2. Resolve the plan<br/>Plain code verifies phrases against<br/>NCTQ's reviewed catalog."]
        R -.->|Only for an ambiguous name| ADJ["Catalog adjudicator<br/>Chooses only from supplied candidates."]
        R --> X["3. Fetch the facts<br/>Plain database queries."]
        X --> RD["4. Assemble the answer<br/>Plain code creates facts, tables,<br/>citations, and downloads."]

        ROUTE -->|Needs clarification| C["Clarify<br/>The writing model may phrase a follow-up.<br/>A fixed fallback is always available."]
        ROUTE -->|Direct reply| D["Build direct response<br/>Plain code"]
        ROUTE -->|Policy or publication| N["Retrieve approved NCTQ material<br/>and render a grounded response."]

        RD --> STYLE{Eligible for optional<br/>writing polish?}
        N --> STYLE
        STYLE -->|No| OUT([Response sent])
        STYLE -->|Yes| S["Answer stylist<br/>May improve wording only;<br/>facts and citations stay locked."]
        S --> G{Rewrite passes validation?}
        G -->|Yes| OUT
        G -->|No: use deterministic version| OUT
        C --> OUT
        D --> OUT
    end

    API --> M
    X --- DB[(NCTQ reviewed data)]
    OUT -.->|Afterward, in the background| Q["Quality evaluation<br/>Models select relevant checks and grade the response.<br/>They never edit or block it."]

    classDef planning fill:#dbeafe,stroke:#2563eb,color:#111827
    classDef writing fill:#f3e8ff,stroke:#9333ea,color:#111827
    classDef judging fill:#dcfce7,stroke:#16a34a,color:#111827
    classDef code fill:#f3f4f6,stroke:#4b5563,color:#111827
    classDef endpoint fill:#fef3c7,stroke:#d97706,color:#111827

    class P planning
    class C,S writing
    class ADJ,Q judging
    class FE,API,M,R,X,RD,D,N,G,DB code
    class U,OUT endpoint
```

A turn's stages in code: session load → planner → context merge and normalization →
catalog resolution → execution and rendering → persistence. The orchestration
entrypoint is `build_chat_response()` in
[`backend/src/compass_backend/orchestration/chat.py`](../backend/src/compass_backend/orchestration/chat.py),
written as a table of contents over named stage helpers; the stage order above is
readable directly from it.

## Planning: intent becomes a typed plan

The planner is the single model authority for **what the question is asking**, and
nothing else. Its output is a typed `PlannerTurn` object, not prose. It chooses one
of five routes:

| Route | When | What happens next |
| --- | --- | --- |
| `execute` | A data question | A typed `QueryPlan` runs against the database |
| `clarify` | The question is ambiguous | Compass asks one grounded clarifying question |
| `policy_guidance` | "What does NCTQ recommend…" | Answered from NCTQ's published positions only |
| `publication` | "What has NCTQ written about…" | Answered from the publications catalog only |
| `direct` | Greetings, meta-questions | A direct reply, no data fetch |

Three rules keep planning safe:

- **No prose dispatch.** Nothing downstream branches on natural-language strings;
  routing happens on typed fields. A CI test enforces this with a
  shrinking baseline of grandfathered exceptions.
- **No minted identifiers.** The planner emits *phrases* ("Denver", "starting
  salary"); it cannot supply database IDs, SQL, or citations.
- **No silent re-rolls.** The planner runs with zero output retries. If its output
  fails validation, the turn becomes an honest clarifying question rather than a
  quietly regenerated plan.

District facts and NCTQ opinion are never mixed: district data answers come from the
policy dataset, and NCTQ's stances come only from its published positions, each with
its own route.

> **In flight:** today the planner must commit to one query shape before it has
> looked at the data; letting it explore the catalog first, with the same typed
> execution and the same grounding rules, is a known improvement under way. See
> [Known Issues & Limitations](09-known-issues-and-limitations.md#the-planner-picks-a-query-shape-before-it-sees-the-data).

## Retrieval: phrases become verified entities

> **A foundational concept for trusting the answers Compass gives.** Catalog
> resolution is a one-way door between language and data. Compass never acts on
> words: words are exchanged for catalog IDs before any data is touched, and
> everything after the exchange deals only in IDs. The picture to hold is a
> librarian and a writer. The librarian doesn't fetch a book by how well someone
> describes it. The description is exchanged at the card catalog for a call number,
> and the call number retrieves the exact book. The words inside that book are
> sacrosanct, delivered to the reader as printed: the writer may introduce and
> frame them, but not rewrite them. In Compass, the planner's phrases are the
> description, the catalog issues the call number, and the facts that come back are
> the book's contents, carried untouched through the tables, the citations, and the
> CSV download. A phrase the catalog can't exchange stops the plan: Compass asks a
> clarifying question or declines, rather than guessing, because a made-up thing
> has no ID and nothing runs without one. (District names make the exchange a
> little later than metrics and topics do, at query time rather than plan time, but
> the rule is the same everywhere.)

In code, the card catalog is the **CatalogResolver**. The planner's phrases carry
no authority of their own; the resolver turns them into real identifiers or blocks
the plan:

- District, metric, and topic phrases are reconciled against the catalog. Exact
  matches and curated aliases resolve in plain code; a genuinely ambiguous phrase
  goes to a bounded AI adjudicator that may only choose among the candidates it
  is given and cannot add one of its own.
- Every resolution is recorded in a `CatalogResolutionReport` (phrase, method,
  approved IDs, candidates, blockers) and saved with the turn, so each answer
  carries its own audit trail.
- The coverage universe, the set of districts Compass can speak about at all,
  derives from the `district_profiles` view rather than from hardcoded lists or
  prompt text.
- Questions about concepts NCTQ hasn't reviewed (registered as out-of-universe
  aliases) are refused honestly instead of answered loosely.

The same discipline applies to NCTQ's supporting materials. A data answer can carry
NCTQ context (a relevant rationale, exemplar policy, or publication) as clearly
labeled asides, capped at two, each with its source link. Publications behave like
data: the renderer may only name publications the query actually fetched, and a
manifest validator rejects any cited title or URL outside that set.

## Generation: facts first, phrasing second

> **A foundational concept for trusting the answers Compass gives.** By this point
> the hard judgment calls are behind Compass. It knows what kind of question it is
> answering, and every district, metric, and topic has been exchanged for a
> verified catalog ID, so it knows exactly what it is working with. Building the
> answer is designed to involve as little decision-making as possible from here:
> the way to set an AI system up for success is to limit its options, and by
> answer time Compass has almost none left. Ordinary database queries fetch the
> facts. Plain code, with no AI model anywhere in it, lays them out as the answer:
> the lead sentence, the tables, the citation markers, the CSV download. Given the
> same plan and the same data, this stage produces the same result every time. In
> the librarian-and-writer picture, this is the moment between the two: the exact
> books are on the desk, and the answer is assembled straight from their pages
> before the writer writes a word. What comes out is a finished briefing prepared
> for the writer, facts checked and a source pinned to every value. The writer's
> turn is next, and wording is the only thing left in its hands.

In code, execution is deterministic: typed operations (lookup, ranking, count, trend, peer
comparison, and so on) run against read-only views of the `compass` schema. The
**renderer** (plain Python, no model) assembles the answer skeleton: a lead
sentence, data tables with a Sources column, coverage notes, and a downloadable CSV
artifact whose rows match the table exactly (same values, same citations; if there
are no rows, no CSV is offered).

When a response qualifies for polish, the **answer stylist** rewrites the prose for
a human reader. It works inside a sealed brief: the facts, tables, caveat lines, and
source blocks are immutable, and the style guide's hard rules forbid adding facts,
numbers, or markers, rounding or computing values, or inventing NCTQ positions.
Validation enforces the contract mechanically:

- **Numeric-token provenance:** every number in the styled text must exist in the
  sealed facts; a rewrite that adds or changes a number is rejected.
- **Citation-marker integrity:** markers can't be invented or reattached.
- **Fallback, always available:** if the stylist times out or fails validation,
  the deterministic rendering ships instead. A styled answer is never required for
  correctness.

> **On the punch list:** two related items live in
> [Known Issues & Limitations](09-known-issues-and-limitations.md): today's final
> check [catches added facts, not dropped ones](09-known-issues-and-limitations.md#the-final-check-catches-added-facts-not-dropped-ones)
> (and how sealing mitigates that), and the planned upgrade to
> [a writer that composes from the full data, behind a fact-coverage gate](09-known-issues-and-limitations.md#a-writer-that-composes-from-the-full-data-behind-a-fact-coverage-gate).

## Citations

The executor builds citation markers from the evidence rows behind each data cell
(source document, page, URL), deduplicates them by URL, and attaches them to the
result before any model sees the answer. The stylist cannot add or move them. Markers are
re-derived fresh on every turn from that turn's rows; there is no session-level
citation registry. The same citation columns ride along in the CSV export, so an
analyst opening the file offline still sees where every value came from.

## Answer structure

A Compass data answer has a consistent shape:

1. **Lead:** a direct answer to the question asked, echoing the user's terms.
2. **Coverage honesty before the data:** if something is missing (a district not
   reviewed this year, a topic not applicable), the answer says so *before* the
   table, in canonical phrasing that the stylist must preserve verbatim.
3. **The table:** with a Sources column of citation markers.
4. **Downloads:** a CSV of exactly the table's rows; charts when appropriate.
5. **Optional NCTQ aside:** at most two labeled, linked snippets of NCTQ guidance,
   and only when the answer has room for them.

Research and policy questions (`policy_guidance`, `publication` routes) return a
summary drawing only on NCTQ's published resources, with the same citation
discipline.

## Prompts and instructions: where they live, how they work

All model instructions are markdown files in this repository under
[`backend/src/compass_backend/instructions/`](../backend/src/compass_backend/instructions/),
loaded through one cached loader. Two tiers:

- **Base instructions: always on, one per agent.**
  [`model_instructions/planner.md`](../backend/src/compass_backend/instructions/model_instructions/planner.md)
  (the planner's contract, routing rules, and examples),
  [`judge.md`](../backend/src/compass_backend/instructions/model_instructions/judge.md) (quality
  judging), plus
  [`answer_style_guides/default.md`](../backend/src/compass_backend/instructions/answer_style_guides/default.md)
  (the stylist's voice and hard rules). The catalog adjudicator and other small
  agents have their own.
- **Planner guidance: on demand, selected per question.** Small topic snippets in
  [`planner_guidance/`](../backend/src/compass_backend/instructions/planner_guidance/)
  are chosen by a deterministic selector: word-boundary trigger
  phrases, blocked phrases, prior-route requirements, and priorities, capped at
  three snippets per question. Selected guidance is injected with an explicit
  warning that it carries no execution, catalog, or citation authority, and the
  selection is persisted with the turn for auditability.

The division of labor is strict: *facts live in code* (IDs, coverage truth,
selection rules, validators, renderer decisions); instruction files own phrasing,
routing judgment, and voice. A house style guide and lint tests keep the instruction
files consistent. Because the files are in git, their version history **is** the
prompt version history.

## How Compass uses different AI models

Compass uses different AI models for different jobs inside a single turn. The model
that interprets the question is not the one that polishes the final wording or
grades quality afterward. The current default assignments are Anthropic Claude
models, routed through the Pydantic AI Gateway:

| Role | What it does | Default model |
| --- | --- | --- |
| Planner | Interprets the question and produces the typed plan. | Claude Sonnet 4.6 |
| Answer and clarification stylist | Optionally polishes validated text or phrases a follow-up question. | Claude Opus 4.6 |
| Catalog adjudicator | Chooses among supplied candidates when a name is ambiguous. | Claude Haiku 4.5 |
| Criterion classifier and quality judges | Select relevant checks and grade the response after it ships. | Claude Haiku 4.5 |

The reasoning behind the split:

- Planning gets the strongest model for structured reasoning. This is the
  highest-stakes model step: a wrong route or invalid plan sends the rest of the
  turn down the wrong path.
- Small, bounded judgments get a small, fast model. The adjudicator, classifier,
  and judges only choose among options Compass supplies, so speed and cost matter.
- Writing polish is optional by design. If the stylist fails, times out, or its
  rewrite fails validation, the deterministic validated answer ships instead. The
  clarification stylist also has a fixed fallback question.
- Any role can change models without changing the system. Each role is an
  independent setting, so a candidate model is evaluated for that job and adopted
  only if it performs better on the relevant reliability, quality, speed, and cost
  measures.

## Voice and tone

The voice standard lives in
[`answer_style_guides/default.md`](../backend/src/compass_backend/instructions/answer_style_guides/default.md)
and is summarized as a
*plain-spoken explainer talking to a policy reader who doesn't need to be eased into
the data*: lead with the answer, echo the user's words, name what's missing without
apology, offer one observation the data invites, use contractions, skip preambles
and promotional language. Coverage language is canonical and verbatim (for example,
*"Issue not addressed in the documents reviewed."*), so the same situation is always
described the same way. Internal jargon (metric slugs, "in-scope cells") is banned
from user-facing text. Voice is tuned by editing the style guide only, which is why
voice changes are expected to move zero accuracy scores.

## Quality, after the answer

Every turn is also judged after the response ships: a classifier selects the
relevant criteria and quality judges record pass/fail verdicts to an append-only
ledger. This is diagnostic: it powers the scorecard and the evaluation loop in
[Quality & Evaluation](04-quality-and-evaluation.md), and it never blocks or edits
an answer mid-turn.

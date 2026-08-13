# Evaluation results — 2026-08-12 ledger export

**Export:** 2026-08-12T23:19:46Z · **Ledger span:** 2026-05-17 → 2026-07-07 ·
**Companion to:** [§4 Quality & Evaluation](../04-quality-and-evaluation.md)

This is the dated evidence behind every number in §4. It exists so the
narrative there can stay readable while the sweep IDs, denominators, method
notes, and caveats live somewhere a skeptical reader can check. When the
ledger is re-exported, this file is superseded by a new dated file, not
edited in place.

**Not a release certification.** The ledger span ends 2026-07-07 because the
project moved into documentation and handoff after that date, not because
evaluation stopped finding things to fix. Two different "as of" dates appear
below by design: sweep results are dated to when each sweep *finished*;
scenario/case/criterion catalog counts are dated to the *export timestamp*,
because those rows are mutable and their current state cannot prove what they
looked like on July 7.

## How to read a claim's provenance

Every number here carries one of these labels:

- **ledger-derived** — recomputed from the exported evaluation ledger; the
  method and sweep IDs are stated with it.
- **report-derived** — quoted from a dated internal audit or campaign record;
  the export could not independently reproduce it (usually because the
  original used a narrower filter or a rolling time window), so it is kept
  with its original date rather than silently promoted or dropped.
- **human-adjudicated** — a reviewer's classification, preserved as judgment
  rather than recomputed as arithmetic.

## The export itself

The staging evaluation ledger was exported on 2026-08-12 with a read-only,
checksummed exporter (single repeatable-read transaction; per-file SHA-256
checksums; foreign-key integrity checks). Row counts matched live database
counts exactly at export time — zero drift:

| Table | Rows | Date range |
| --- | ---: | --- |
| scenarios | 391 | 2026-05-17 → 2026-07-07 |
| cases | 425 | 2026-05-17 → 2026-07-07 |
| criteria | 100 | 2026-05-17 → 2026-07-07 |
| sweep_runs | 413 | 2026-05-20 → 2026-07-07 |
| verdicts | 471,489 | 2026-05-17 → 2026-07-07 |

The row-level corpus stays out of this repository by policy: verdict rows
carry judge-written reasoning that can quote answer text, and case fixtures
carry conversation setups. Aggregates, identifiers, dates, and codes — what
this file contains — are the auditable surface.

## Scoring method

All scores below use the application's own scorecard math, replayed against
the export (not a simpler pass/fail ratio):

1. **Pinning.** Each dimension's headline sweep is its most comprehensive
   completed run: `case_count DESC, finished_at DESC`. Sweeps that ended
   `partial` or `failed` are never scored.
2. **Exclusions.** Trials are dropped from both numerator and denominator
   when they are stub errors (unwired deterministic validators),
   "not applicable" outcomes (a validator fired on a turn it had no
   constraint for), or infrastructure-skip errors. What remains are the
   *evaluable trials*.
3. **Macro-average.** Pass rate per case (across its trials, often K=3),
   then mean across a scenario's cases, then mean across the dimension's
   scenarios. This keeps one heavily-tested case from dominating a
   dimension.
4. **No blending.** No score mixes verdicts across sweep runs or across
   build/grader fingerprints.

## Per-dimension results (pinned sweeps)

Ledger-derived. Each row is one dimension's single most-comprehensive
completed sweep as of the export — not a hand-picked best run.

| Dimension | Score | Target | Evaluable trials | Cases | Sweep finished | Sweep ID | Build |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| Sort Accuracy | 99% | 98% | 919 | 39 | 2026-06-22 | `289428b0-d34d-4904-b562-9e48f203dae2` | `33b7898a` |
| Citation Accuracy | 92% | 98% | 1,606 | 42 | 2026-06-15 | `f6d0fe3c-3c10-420e-8887-b390ff39e788` | `ccf381e7` |
| Consistency | 89% | 95% | 1,467 | 28 | 2026-05-26 | `8f5274bf-e855-4b87-bf23-9d3e9cae3484` | `fea88df9` |
| Filter Accuracy | 73% | 95% | 599 | 30 | 2026-06-22 | `de1433e2-821d-4388-a3f4-00ebc2318876` | `796ad624` |
| Coverage-State Labeling | 68% | 98% | 823 | 38 | 2026-06-22 | `22e7c30d-bb2d-47fb-8beb-151eb23a4948` | `796ad624` |
| Data Fidelity | 62% | 99% | 1,236 | 81 | 2026-06-22 | `1bddcce2-4eab-4e5c-817e-f6aa40654d9d` | `796ad624` |
| Selection Accuracy | 47% | 95% | 184 | 97 | 2026-06-22 | `7a80b3cd-6d87-4d44-816c-d5e805d3a59b` | `796ad624` |

The seven pinned sweeps span 2026-05-26 to 2026-06-22; read that span, not a
single date, as the results' "as of." Two retired dimensions also exist in
the ledger — Process Integrity and Surface Consistency, run only in May 2026
on build `b-2026-05-14` — and are excluded from every seven-dimension figure
in this file and in §4.

**The Selection caveat.** The pinned Selection sweep ran only the holistic
scenario-fit judge, the strictest single lens in the library. Its sibling
sweep the same day (`b7110ac3`, same 97 cases, same build `796ad624`) ran the
structured checks and read 65%. A week earlier, the broadest Selection
measurement in the ledger (`af9c64a3`: all four checker types, K=3, 4,954
evaluable trials) read 93%. None of these is "the" score; each is one
instrument's reading. See the trajectories below.

## The scenario library over time

Ledger-derived (catalog counts as of the export timestamp). The 2026-06-09
internal audit counted **326 active scenarios**; the export shows **390
active of 391** — the same library at two points on its growth curve, not a
conflicting count. Per-dimension at export time:

| Dimension | Active | Total |
| --- | ---: | ---: |
| Selection Accuracy | 107 | 108 |
| Data Fidelity | 83 | 83 |
| Citation Accuracy | 45 | 45 |
| Coverage-State Labeling | 37 | 37 |
| Filter Accuracy | 32 | 32 |
| Sort Accuracy | 28 | 28 |
| Consistency | 36 | 36 |
| Voice & Tone | 4 | 4 |
| Selection *(legacy label predating the "Selection Accuracy" rename)* | 1 | 1 |
| Process Integrity *(retired)* | 15 | 15 |
| Surface Consistency *(retired)* | 2 | 2 |

## Score trajectories and measurement consistency

Ledger-derived, same scoring method as above, applied to every
*comprehensive* completed sweep of a dimension over time (`case_count >= 40`
for Selection, `>= 20` for Sort; below those floors the runs are targeted or
debug sweeps, not board measurements).

Instrument churn across the whole ledger: **122 distinct criterion-set
fingerprints** and **111 distinct product builds** across the 413 sweeps.
Criteria were created 32 in May, 34 in June, and 34 in July (100 total). The
grader and the product were changing nearly continuously at the same time,
which is why almost no two rows below are a controlled comparison — the
tables document the program's volatility honestly rather than presenting a
clean trend line that doesn't exist.

Selection Accuracy:

| Finished | Sweep | Cases | Score | Evaluable | K | Build | Checker mix |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 2026-05-22 | `ee9f8832` | 48 | 54% | 1,658 | 3 | `dc52cec5` | pre-checker-type era |
| 2026-05-22 | `e4293ccb` | 48 | 54% | 1,678 | 3 | `dc52cec5` | pre-checker-type era |
| 2026-05-22 | `6573796f` | 48 | 98% | 1,665 | 3 | `9a075801` | pre-checker-type era |
| 2026-05-22 | `beb3a47c` | 48 | 100% | 1,778 | 3 | `6ac57b50` | pre-checker-type era |
| 2026-05-23 | `54f52554` | 47 | 99% | 2,089 | 3 | `a832d72f` | pre-checker-type era |
| 2026-05-26 | `1cbcd657` | 74 | 87% | 2,697 | 3 | `fea88df9` | pre-checker-type era |
| 2026-06-09 | `5ec9a47f` | 92 | 98% | 1,481 | 1 | `593758f1` | deterministic, judge, telemetry |
| 2026-06-14 | `2007d55f` | 92 | 97% | 1,571 | 1 | `be38a566` | deterministic, judge, telemetry |
| 2026-06-15 | `af9c64a3` | 92 | 93% | 4,954 | 3 | `ccf381e7` | all four |
| 2026-06-22 | `b7110ac3` | 97 | 65% | 1,738 | 1 | `796ad624` | deterministic, judge, telemetry |
| 2026-06-22 | `7a80b3cd` | 97 | 47% | 184 | 1 | `796ad624` | holistic judge only |

The 54% → 100% jump within hours on May 22 is a grading-era fix, not a
product transformation. The June 22 pair is the two-lens demonstration:
same day, same 97 cases, same build — two instruments, two numbers.

Sort Accuracy:

| Finished | Sweep | Cases | Score | Evaluable | K | Build |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 2026-05-26 | `241157c8` | 26 | 86% | 734 | 3 | `fea88df9` |
| 2026-05-27 | `275cab71` | 32 | 72% | 1,719 | 3 | `71b72b23` |
| 2026-05-28 | `7399d278` | 32 | 49% | 1,663 | 3 | `b811497f` |
| 2026-06-05 | `de991d78` | 33 | 89% | 2,450 | 3 | `380e4493` |
| 2026-06-09 | `0a08becc` | 34 | 99% | 648 | 1 | `593758f1` |
| 2026-06-14 | `c51bd423` | 39 | 99% | 1,012 | 1 | `be38a566` |
| 2026-06-15 | `912bdce4` | 39 | 96% | 3,151 | 3 | `ccf381e7` |
| 2026-06-22 | `289428b0` | 39 | 99% | 919 | 1 | `33b7898a` |

Sort is the held-instrument contrast: after the May 28 trough it converged
to 99% and stayed there across June while the case set grew from 34 to 39.
Where the instrument was held steady, the improvement is visible and durable.

## How the evaluation program got more stringent (April–June 2026)

The team cannot rerun the April/May product against June's criteria (the
export reconstructs saved runs; it does not run new ones), so there is no
clean isolated judge-only delta. The dated record does show the instrument
maturing, in order:

1. **2026-06-21 — criterion precision review** *(human-adjudicated +
   report-derived)*. A criterion-by-criterion review of the live verdict
   ledger found most recorded failures were real product gaps, not judge
   miscalibration — and found the opposite blind spot: of 10 cases in the
   window where a refusal or canned rescue actually occurred, the holistic
   judge had passed 7. The response was to build a new deterministic check
   for that recall hole (`answerability_rescue_fallback_is_failure`) rather
   than loosen anything.
2. **2026-07-03 → 07-05 — the answerability-ladder campaign**
   *(report-derived; its three named closure sweeps — Selection `41d82d44`,
   Citation `b8aa103c`, Coverage `666b2d9f` — are confirmed present and
   completed in the export)*. Three waves closed dead-ends the stricter
   criteria had made visible. The campaign record cites 11 target criteria
   at 100% over K=3 replays and 19/19 post-deploy case passes; those figures
   used a narrower "target criteria on their intended cases" filter that
   this export pass did not independently re-derive, so they stay
   report-derived with their dates.
3. **2026-07-06 — footnote precedence flip-set** *(report-derived; a
   citation data-loader audit, outside the verdict ledger entirely)*. A
   precision-first citation rule flipped 393 rows and deliberately withheld
   22, rather than shipping a cruder 415-row rule that would have displaced
   real linked documents on 8 rows.

This timeline is why the pinned-sweep table above should not be read as a
regression from the historical "~98.6% clean board" note (2026-06-09,
report-derived): that figure predates the precision review and used an
earlier, less stringent criterion set. It is not reproducible from this
export, and the two are not comparable.

## Worked-example evidence bundle

Ledger-derived. The §4 worked example, with its identifiers:

- **Scenario** `REGR-M1-SCHOOL-DAYS-DC-ANCHOR` (id 1016, Filter Accuracy) →
  **case** `REGR-M1-SCHOOL-DAYS-DC-ANCHOR-C00` (id 1028).
- **Failing verdict:** criterion `answerability_rescue_fallback_is_failure`
  recorded `fail` on this case on 2026-06-16 (sweep `ab613cc1`), alongside
  `SCENARIO_FIT_001` failing from 2026-05-26 through 2026-05-28 (sweeps
  `332e118d`, `b3acadee`, `62003c23`).
- **The fix:** a dedicated regression criterion,
  `school_days_anchor_clarifies_not_rescue`, was added to the ledger on
  2026-06-20 to pin the corrected behavior. It falls inside the
  answerability-ladder campaign's evaluation-ledger changes; the ledger
  verifies the criterion and its dates, not which specific commit authored
  it.
- **Passing replay:** both criteria pass on every recorded verdict for this
  case from 2026-06-20 onward, including in `de1433e2` — the sweep pinned as
  Filter Accuracy's headline result above. That same sweep shows the case
  failing its `trace_id_present` observability check — a correct-but-
  unobservable turn, reported as its own axis rather than folded into
  product failures.
- **Scope note:** the same regression criteria do record failures when the
  evaluation classifier applies them to *other* cases they were never
  written for (criteria over-application — see
  [§9's evaluation-program list](../09-known-issues-and-limitations.md#evaluation-program)).
  On their home case, they hold.

## Refreshing this file

Re-export the ledger with the team's internal exporter (policy-advisor repo,
`scripts/pa_eval/export_evidence_corpus.py`: staging-only, read-only,
checksummed, with a `--verify` mode), recompute the tables above with the
same pinning and macro-average method, and add a new dated file beside this
one. Comparisons are only apples-to-apples where the criterion set, build,
and checker mix are held fixed — state all three with any new number.

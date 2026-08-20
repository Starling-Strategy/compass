# Databricks Notebook Inventory

This is the operational inventory requested in the client outline: **47 notebooks
across nine folders** supporting the NCTQ data platform and Compass. It is based on
internal NCTQ Databricks platform documentation dated **July 6, 2026**.

This is a source-based handoff, not a live Databricks or Azure Data Factory export.
The source does not provide the exact workspace filename for each of the 13 TCD
table-copy notebooks, does not name one of the six document-pipeline notebooks, and
does not assign a named owner to every notebook. Those entries are marked explicitly
so they can be reconciled against the live workspace before this becomes the
authoritative runbook.

No credentials, tokens, connection strings, or secret values belong in this file.

## Platform summary

| Item | Source-backed detail |
| --- | --- |
| Scheduler | Azure Data Factory, `master_daily` pipeline |
| Nightly trigger | 1:00 AM Eastern; dependent steps wait for earlier steps to succeed |
| Workspace structure | 9 folders, 47 notebooks |
| Main Compass destination | Production PostgreSQL, `compass` schema |
| Shared data model | Bronze = source copy; Silver = cleaned and enriched; Gold = application-ready; Production = PostgreSQL |
| Failure behavior | Failed scheduled steps stop dependent work and send email/Slack alerts to the data team |
| Operational owner | NCTQ data team; the source names a member of the NCTQ data team as the nightly-report recipient, not as every notebook's owner |

## Folder and pipeline map

| Order | Folder | Count | Schedule / dependency | Responsibility |
| ---: | --- | ---: | --- | --- |
| 0 | `databricks_config` | 1 | One-time setup | Create Bronze/Silver/Gold schemas |
| 1 | `nctq_clone` | 15 | Nightly first step; table copies run in parallel | Copy TCD tables and views into Bronze |
| 2 | `nctq_sql_delta` | 1 | Daily after the TCD copy | Track `answer_options` changes |
| 3 | `compass_sync` | 12 | Nightly; extractors 1–8 parallel, then validation and push | Build and deliver Compass data |
| 4 | `document_pipeline` | 6 | Processing/audit track; exact trigger not supplied | Extract and enrich policy documents |
| 5 | `nctq_web_db` | 3 | Approximately 5:00 AM Eastern | Export tables/views and verify row counts |
| 6 | `production` | 6 | On demand after AI review | Write approved AI-reviewed answers back to TCD |
| 7 | `railway_clone` | 2 | Scheduled or on demand; exact trigger not supplied | Archive Railway/NocoDB data and feed web tables |
| 8 | `staging` | 1 | On demand for comparison | Pull staging Silver data into Databricks |

The functional nightly sequence is: TCD clone → answer-option change tracking →
Compass Sync → web-database export. The other folders have separate setup,
validation, or on-demand workflows.

## Notebook inventory

Each row records the notebook's name (or the source's logical name where the exact
workspace name was not supplied), folder, purpose, schedule/order, inputs, outputs,
and owner.

### `databricks_config` — 1 notebook

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| **Name not supplied — schema setup** | Create or repair the three data-layer schemas; safe to rerun | One-time environment setup | Workspace configuration → Bronze, Silver, Gold schemas | Data engineering; named owner not supplied |

### `nctq_clone` — 15 notebooks

The source says 13 notebooks each copy one TCD table but does not provide their
workspace filenames. The logical table-copy identifiers below are not claims about
the exact Databricks names.

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| **Name not supplied — copy `district`** | Copy TCD district data | Nightly, parallel | TCD `district` → Bronze district table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `district_answers`** | Copy district answers | Nightly, parallel | TCD `district_answers` → Bronze district-answers table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `district_acad_yr`** | Copy academic-year data | Nightly, parallel | TCD `district_acad_yr` → Bronze academic-year table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `district_nces`** | Copy district/NCES links | Nightly, parallel | TCD `district_nces` → Bronze district-NCES table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `district_sources`** | Copy district/source relationships | Nightly, parallel | TCD `district_sources` → Bronze district-sources table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `district_source_yrs`** | Copy source-document years | Nightly, parallel | TCD `district_source_yrs` → Bronze source-years table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `policy`** | Copy policy records | Nightly, parallel | TCD `policy` → Bronze policy table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `subpolicy`** | Copy subpolicy records | Nightly, parallel | TCD `subpolicy` → Bronze subpolicy table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `subpolicy_question`** | Copy subpolicy/question relationships | Nightly, parallel | TCD `subpolicy_question` → Bronze relationship table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `question`** | Copy policy questions | Nightly, parallel | TCD `question` → Bronze question table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `answer_citations`** | Copy answer citations | Nightly, parallel | TCD `answer_citations` → Bronze answer-citations table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `answer_options`** | Copy answer choices | Nightly, parallel | TCD `answer_options` → Bronze answer-options table | NCTQ data team; named owner not supplied |
| **Name not supplied — copy `answer_release_yrs`** | Copy answer release years | Nightly, parallel | TCD `answer_release_yrs` → Bronze release-years table | NCTQ data team; named owner not supplied |
| `data_extract_nctq_all_views` | Copy analyst-facing TCD views, including `vTCD`, `qryTCD`, and `webTCD` views | Nightly with the TCD copy | TCD SQL views → Databricks views schema | NCTQ data team; named owner not supplied |
| `data_describe_nctq_schemas` | Catalog TCD tables and views for setup | One-time/setup | TCD schema metadata → schema catalog | Data engineering; named owner not supplied |

### `nctq_sql_delta` — 1 notebook

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| `delta_answer_options_change_tracking` | Record additions, removals, and edits to `answer_options` | Daily after TCD clone | Current answer-options copy and prior history → Silver change history | NCTQ data team; named owner not supplied |

### `document_pipeline` — 6 notebooks

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| `document_extract` | Download PDFs and convert them to structured text with Docling | Processing track; exact trigger not supplied | Source URLs/blob documents → extracted text and metadata | NCTQ data team; named owner not supplied |
| `document_enrich` | Classify documents and produce titles, summaries, year coverage, and quality ratings | After extraction | Extracted text/metadata → enriched Silver metadata | NCTQ data team; named owner not supplied |
| `document_gap_analysis` | Find pending, failed, or not-yet-enriched documents | After extraction/enrichment | Bronze inventory and Silver records → gap report | NCTQ data team; named owner not supplied |
| `silver_bronze_src_link_audit` | Compare links/names across Databricks layers and correct approved differences | Audit track; exact trigger not supplied | Bronze/Silver document links → diff report/corrections | NCTQ data team; named owner not supplied |
| `src_link_audit` | Compare Databricks document links/names with production PostgreSQL | Audit track; exact trigger not supplied | Databricks catalog and production records → production-link diff/corrections | NCTQ data team; named owner not supplied |
| **Name and role not supplied — sixth notebook** | Confirm from the live workspace and ADF | Verify before operational use | Verify from workspace → verify from workspace | Owner not supplied |

### `compass_sync` — 12 notebooks

The source numbers these as README, setup, 1–8, 10, and 11; number 9 is not listed.

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| `README` | Explain the folder and run order | Reference only | Folder contents → operating instructions | NCTQ data team; named owner not supplied |
| **Name not supplied — schema setup** | Create Compass Sync tables | One-time setup | Workspace/database configuration → empty Compass Sync tables | Data engineering; named owner not supplied |
| **Name not supplied — districts and topics** | Pull district metadata and topics | Nightly extractor 1, parallel | TCD views → `navigator_districts`, `navigator_topics` | NCTQ data team; named owner not supplied |
| **Name not supplied — metrics, answers, and citations** | Pull policy questions, answers, and citations | Nightly extractor 2, parallel | TCD views → `navigator_metrics`, `navigator_answers`, `navigator_citations` | NCTQ data team; named owner not supplied |
| **Name not supplied — source documents** | Pull documents supporting policy answers | Nightly extractor 3, parallel | TCD/document catalog → `navigator_sources` | NCTQ data team; named owner not supplied |
| **Name not supplied — Pathfinder guidance** | Pull guidance from nctq.org WordPress | Nightly extractor 4, parallel | WordPress API → `pathfinder_content_items` | NCTQ data team; named owner not supplied |
| **Name not supplied — NCTQ publications** | Pull publications approved for chatbot use | Nightly extractor 5, parallel | Airtable publication view → publication table | NCTQ data team; named owner not supplied |
| **Name not supplied — NCES districts** | Pull district context data | Nightly extractor 6, parallel | Urban Institute NCES API → NCES district table | NCTQ data team; named owner not supplied |
| **Name not supplied — NCES link and allowlist** | Link NCTQ districts to NCES and restrict fields | Nightly extractor 7, parallel | TCD districts + NCES data → links and field allowlist | NCTQ data team; named owner not supplied |
| **Name not supplied — enrollment authority** | Apply the NYC enrollment rollup | Nightly extractor 8, parallel | NYC/NCES records + authority rules → enrollment-authority values | NCTQ data team; named owner not supplied |
| **Name not supplied — validation** | Check row counts, required columns, uniqueness, and schema shape | Nightly after extractors; blocks push on failure | Compass outputs + last-known-good counts → validation gate | NCTQ data team; named owner not supplied |
| **Name not supplied — push to production** | Merge tables, clean stale rows, refresh views, audit, and report | Nightly after validation | Validated Compass outputs → production `compass` schema, views, audits, email/Slack report | NCTQ data team; report recipient named as a member of the NCTQ data team |

### `nctq_web_db` — 3 notebooks

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| `data_push_bronze_to_web_db` | Push raw TCD copies to the NCTQ web database | Approximately 5:00 AM Eastern | Bronze TCD tables → web database raw tables | NCTQ data team; named owner not supplied |
| `data_push_views_to_web_db` | Push processed views to the NCTQ web database | After Bronze push | Databricks views → web database views | NCTQ data team; named owner not supplied |
| `test_psql_row_count` | Confirm expected counts landed | After both web pushes | Web database tables → row-count check | NCTQ data team; named owner not supplied |

### `production` — 6 notebooks

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| `prod_extract_silver` | Pull AI-generated intermediate data | On demand after AI review | Production Silver → Databricks Silver copy | NCTQ data team; analyst review required |
| `prod_build_gold` | Keep approved answers, join citations, and validate rows | After extract | AI Silver → `gold.ai_reviewed_answers` | NCTQ data team; analyst review required |
| `prod_build_comparison` | Compare approved answers to current TCD and classify actions | After Gold build | Gold answers + TCD answers → comparison/action records | NCTQ data team; analyst review required |
| `prod_push_gold` | Transactionally write approved changes back to TCD | After comparison review | Pending comparisons → TCD updates, audit, email | NCTQ data team; analyst approval required |
| `prod_remove_src_doc` | Preview and remove an invalid source document when authorized | On demand utility | Source-document ID + confirmation → deletion and audit | NCTQ data team; explicit human confirmation required |
| `prod_test_push` | Preview a limited push without changing TCD | On demand before real push | Selected pending answers → analyst preview table | NCTQ data team; analyst review required |

### `railway_clone` — 2 notebooks

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| `railway_extract` | Copy Railway/NocoDB tables into an archive | Scheduled or on demand; exact trigger not supplied | Railway PostgreSQL → Databricks archive schema | NCTQ data team; named owner not supplied |
| `railway_push_archive` | Push selected NCTQ archive tables to the web database | After `railway_extract` | Archive tables → selected web-database tables | NCTQ data team; named owner not supplied |

### `staging` — 1 notebook

| Notebook | Purpose | Schedule/order | Inputs → outputs | Owner |
| --- | --- | --- | --- | --- |
| `staging_extract` | Pull staging Silver for environment comparison | On demand for validation/promotion | Staging PostgreSQL Silver → Databricks staging copy | NCTQ data team; named owner not supplied |

## Controls and follow-up verification

- Validation checks row counts, required non-null columns, key uniqueness, and
  schema shape before Compass data is pushed to production.
- The production push merges data, removes stale rows, refreshes materialized views,
  writes audit rows, and sends a report with the SQL log attached.
- The AI-reviewed-answer flow retains human review and uses transactions before
  writing back to TCD.
- The source describes email, Slack, ADF history, and audit tables as failure
  signals; it does not claim a pager or on-call rotation.

Before treating this as a live runbook, reconcile it against the workspace:

- [ ] Export all nine current folder paths and all 47 exact notebook names.
- [ ] Map each notebook to its actual ADF activity and dependency graph.
- [ ] Name the sixth `document_pipeline` notebook.
- [ ] Replace “owner not supplied” with accountable ownership where desired.
- [ ] Verify schedules, outputs, alert recipients, and current counts.

**Source:** internal NCTQ Databricks platform documentation, July 6, 2026.

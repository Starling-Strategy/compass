# PiedPiper File Mapping

How our `prediction_pipeline/` maps to Nathan's [PiedPiper](https://github.com/Nathan-Roll1/PiedPiper) repo.

## File-by-File

| Our File | Nathan's Equivalent | Notes |
|---|---|---|
| `config.py` | `PredictionConfig` dataclass | We use Pydantic Settings with `PREDICTOR_` env prefix |
| `models.py` | `prediction_models.py` | Pydantic models; field descriptions serve as Gemini prompts |
| `retriever.py` | `PredictionPipeline._build_vespa_query()` | Vespa hybrid search + Postgres fallback. `build_search_query()` matches Nathan's rich query building |
| `predict.py` | `PredictionPipeline._predict_single()` | PydanticAI structured output vs Nathan's raw Gemini + JSON mode. INA footer enforces Katherine's rule |
| `synthesize.py` | `PredictionPipeline._aggregate_predictions()` | Modal vote + INA consensus override. We add entropy tracking |
| `evaluate.py` | `evaluate_predictions()` | 6-way INA-aware accuracy + type-aware matching (numeric tolerance, multi-select) |
| `evals.py` | _(no equivalent)_ | Our addition: pydantic-evals snapshots for regression testing |
| `db.py` | `database.py` | Raw psycopg2 vs Nathan's SQLAlchemy ORM |
| `run.py` | `PredictionPipeline.run()` | Functional `run_pipeline()` with argparse CLI |

## Concept Mapping

| Nathan's Concept | Our Implementation |
|---|---|
| K-diversity voting (k=2..16, 15 runs) | `synthesize.py`: `modal_vote()`, `is_ina_consensus()` |
| INA consensus threshold | `config.py`: `ina_threshold=9` (per-type overrides in `predict.py`) |
| Rich Vespa query building | `retriever.py`: `build_search_query()` — q_text + focus_terms + target_sections + terminology |
| INA footer (last-instruction-wins) | `predict.py`: `_build_ina_footer()` — answer-type-specific guidance at prompt end |
| Parent question context | `models.py`: `parent_q_text` + `dsubpol_id` on `QuestionContext` |
| AY23 fallback | **NOT implemented** (violates Katherine's rule). Instead: INA audit trail in `run.py` logs when prior year had a value |
| 3-tier TTL caching | Retrieve-once optimization: single Vespa call at `k_max`, then `chunks[:k]` per depth |
| `source="piedpiper"` | `config.py`: `source="piedpiper"`, `model_version="piedpiper-v2.0"` |

## Evaluation Labels

| Nathan's Label | Our Label | Meaning |
|---|---|---|
| TRUE_NEG | INA_CORRECT | Both predicted and golden are INA |
| FALSE_POS | INA_FALSE_POS | Predicted INA, golden has value |
| TRUE_POS | EXACT | Correct non-INA answer |
| WRONG_VALUE | VALUE_DIFFERENT | Both have values, but different |
| _(implicit)_ | INA_FALSE_NEG | Predicted value, golden is INA (**Katherine violation**) |
| _(implicit)_ | NOGOLDEN | No golden answer available |

## Key Differences from Nathan

1. **AI client**: PydanticAI structured output vs raw Gemini + JSON mode + manual parsing
2. **Katherine's rule enforcement**: INA footer with answer-type-specific guidance (Nathan's dropdown guidance says "prefer the option"; we say "INA always wins")
3. **No AY23 fallback**: Katherine explicitly rejected this. We log for analyst review instead
4. **Type-aware evaluation**: 0.5% numeric tolerance, order-independent multi-select comparison
5. **Evaluation snapshots**: Save/load/compare for regression testing (Nathan doesn't have this)
6. **Citation verification**: Fuzzy match against source chunks with quality scoring

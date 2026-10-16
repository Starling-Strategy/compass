"""All database operations for the prediction pipeline.

Pattern: match document_pipeline/db.py — all SQL in one file, psycopg2, RealDictCursor.

PiedPiper equivalent: database.py (SQLAlchemy ORM).
We use raw psycopg2 + RealDictCursor to match the docpipe pattern.
"""
import json
import logging
import re
from contextlib import nullcontext as _nullcontext
from datetime import datetime
from uuid import UUID

import psycopg2
import psycopg2.extras

from prediction_pipeline.config import Config
from prediction_pipeline.models import (
    INA_VARIANTS,
    PredictionRun,
    QuestionContext,
    SuggestedAnswer,
)

try:
    import logfire
except ImportError:
    logfire = None

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# SQL IN-list derived from canonical INA_VARIANTS
_INA_SQL_LIST = ", ".join(f"'{v}'" for v in sorted(INA_VARIANTS))

# Cache: district_id -> {src_id_str -> doc_id_uuid_str}
_doc_id_cache: dict[int, dict[str, str]] = {}


def resolve_doc_ids(config: Config, district_id: int) -> dict[str, str]:
    """Load src_id -> doc_id mapping for a district. Cached per district."""
    if district_id in _doc_id_cache:
        return _doc_id_cache[district_id]

    sql = """
        SELECT src_id::text, doc_id::text
        FROM silver.district_documents
        WHERE district_id = %s AND src_id IS NOT NULL
    """
    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (district_id,))
            rows = cur.fetchall()

    mapping = {row["src_id"]: row["doc_id"] for row in rows}
    _doc_id_cache[district_id] = mapping
    logger.info("Cached %s src_id->doc_id mappings for district %s", len(mapping), district_id)
    return mapping


def resolve_single_doc_id(id_str: str, mapping: dict[str, str]) -> str | None:
    """Return UUID if already UUID, otherwise look up in mapping."""
    if _UUID_RE.match(id_str or ""):
        return id_str
    return mapping.get(id_str)


def get_pg_connection(config: Config):
    """Get a PostgreSQL connection from config."""
    return psycopg2.connect(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=config.pg_user,
        password=config.pg_password,
    )


def load_questions(config: Config, q_ids: list[int] | None = None, priority: str | None = None) -> list[QuestionContext]:
    """Load active questions from silver.questions.

    For conditional "If..." questions, loads parent question text via dsubpol_id
    so the model has context about what the condition refers to.
    """
    sql = """
        SELECT q.q_id, q.q_text, q.q_ans_type, q.effective_valid_options,
               q.allows_ina, q.allows_na, q.effective_coding_guidance,
               q.ai_coding_notes, q.silence_policy,
               q.focus_terms, q.target_sections, q.terminology_context,
               q.synthesis_logic, q.q_priority, q.dsubpol_id,
               parent.q_text AS parent_q_text
        FROM silver.questions q
        LEFT JOIN silver.questions parent
            ON q.dsubpol_id IS NOT NULL
           AND parent.q_id = q.dsubpol_id
        WHERE q.guidance_is_active = true
    """
    params: list = []
    if q_ids:
        sql += " AND q.q_id = ANY(%s)"
        params.append(q_ids)
    if priority:
        sql += " AND q.q_priority = %s"
        params.append(priority)

    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    questions = []
    for row in rows:
        # Parse JSON fields
        valid_options = row["effective_valid_options"]
        if isinstance(valid_options, str):
            valid_options = json.loads(valid_options)
        focus_terms = row["focus_terms"]
        if isinstance(focus_terms, str):
            focus_terms = json.loads(focus_terms)
        target_sections = row["target_sections"]
        if isinstance(target_sections, str):
            target_sections = json.loads(target_sections)

        questions.append(QuestionContext(
            q_id=row["q_id"],
            q_text=row["q_text"],
            q_ans_type=row["q_ans_type"],
            valid_options=valid_options or [],
            allows_ina=row.get("allows_ina", True),
            allows_na=row.get("allows_na", False),
            coding_guidance=row.get("effective_coding_guidance"),
            ai_coding_notes=row.get("ai_coding_notes"),
            silence_policy=row.get("silence_policy"),
            focus_terms=focus_terms,
            target_sections=target_sections,
            terminology_context=row.get("terminology_context"),
            synthesis_logic=row.get("synthesis_logic"),
            q_priority=row.get("q_priority"),
            dsubpol_id=row.get("dsubpol_id"),
            parent_q_text=row.get("parent_q_text"),
        ))
    return questions


def load_prior_year_answers(config: Config, district_id: int, prior_ay_id: int) -> dict[int, str]:
    """Load prior-year golden answers for INA audit trail.

    When we predict INA but the prior year had a value, analysts should review.
    This does NOT override predictions (the silence rule: silence = INA always).
    Returns {q_id: answer_text} for non-INA answers only.
    """
    sql = f"""
        SELECT question_id, answer
        FROM silver.v_golden_answers
        WHERE district_id = %s AND ay_id = %s
          AND answer IS NOT NULL
          AND LOWER(TRIM(answer)) NOT IN ({_INA_SQL_LIST})
    """
    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (district_id, prior_ay_id))
            rows = cur.fetchall()
    return {row["question_id"]: row["answer"] for row in rows}


def load_golden_answers(config: Config, district_id: int, ay_id: int) -> dict[int, str]:
    """Load golden answers from silver.v_golden_answers. Returns {q_id: answer_text}."""
    sql = """
        SELECT question_id, answer
        FROM silver.v_golden_answers
        WHERE district_id = %s AND ay_id = %s
    """
    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (district_id, ay_id))
            rows = cur.fetchall()
    return {row["question_id"]: row["answer"] for row in rows}


def get_district_info(config: Config, district_id: int) -> tuple[str, str]:
    """(district_name, state) from bronze.district."""
    sql = "SELECT district_name, district_state FROM bronze.district WHERE district_id = %s"
    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (district_id,))
            row = cur.fetchone()
    if not row:
        raise ValueError(f"District {district_id} not found")
    return row["district_name"], row["district_state"]


def save_prediction_runs(runs: list[PredictionRun], config: Config) -> None:
    """Batch INSERT to silver.prediction_history using execute_values for speed."""
    if not runs:
        return
    with logfire.span(
        "save_prediction_runs",
        n_rows=len(runs),
        q_id=runs[0].q_id,
        generation_id=str(runs[0].generation_id),
    ) if logfire else _nullcontext():
        sql = """
            INSERT INTO silver.prediction_history (
                district_id, ay_id, q_id, generation_id, top_k,
                predicted_answer, confidence, reasoning, key_citations_json,
                evidence_agreement,
                source, model_version, run_id, predicted_at
            ) VALUES %s
        """
        rows = []
        for run in runs:
            rows.append((
                run.district_id, run.ay_id, run.q_id, str(run.generation_id), run.top_k,
                run.predicted_answer, run.confidence, run.reasoning,
                json.dumps(run.key_citations_json) if run.key_citations_json else None,
                run.evidence_agreement,
                run.source, run.model_version, run.run_id,
            ))

        template = "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())"
        with get_pg_connection(config) as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur, sql, rows, template=template, page_size=100,
                )
            conn.commit()
        logger.info("Saved %s prediction runs (generation=%s)", len(runs), runs[0].generation_id)


def insert_suggested_answer(answer: SuggestedAnswer, config: Config) -> None:
    """Insert a suggested answer row.

    Append-only: PK is (district_id, ay_id, q_id, run_id), so each pipeline
    run creates a new row. The v_latest_suggested_answers view surfaces the
    most recent row per question.
    """
    with logfire.span(
        "insert_suggested_answer",
        district_id=answer.district_id,
        q_id=answer.q_id,
        suggested_answer=answer.suggested_answer,
    ) if logfire else _nullcontext():
        sql = """
            INSERT INTO silver.suggested_answers (
                district_id, ay_id, q_id, suggested_answer, confidence,
                reasoning, citations_json, citation_doc_ids, is_ina,
                generation_id, entropy, agreement_pct, n_unique_answers,
                n_predictions, vote_distribution, match_status,
                status, model_version, source, run_id, batch_id,
                created_at
            ) VALUES (
                %(district_id)s, %(ay_id)s, %(q_id)s, %(suggested_answer)s, %(confidence)s,
                %(reasoning)s, %(citations_json)s, %(citation_doc_ids)s::uuid[], %(is_ina)s,
                %(generation_id)s, %(entropy)s, %(agreement_pct)s, %(n_unique_answers)s,
                %(n_predictions)s, %(vote_distribution)s, %(match_status)s,
                %(status)s, %(model_version)s, %(source)s, %(run_id)s, %(batch_id)s,
                NOW()
            )
        """
        params = {
            "district_id": answer.district_id,
            "ay_id": answer.ay_id,
            "q_id": answer.q_id,
            "suggested_answer": answer.suggested_answer,
            "confidence": answer.confidence,
            "reasoning": answer.reasoning,
            "citations_json": json.dumps(answer.citations_json) if answer.citations_json is not None else None,
            "citation_doc_ids": [
                str(did) for did in {
                    c.get("doc_id") for c in (answer.citations_json or []) if c.get("doc_id")
                }
            ] or None,
            "is_ina": answer.is_ina,
            "generation_id": str(answer.generation_id),
            "entropy": answer.entropy,
            "agreement_pct": answer.agreement_pct,
            "n_unique_answers": answer.n_unique_answers,
            "n_predictions": answer.n_predictions,
            "vote_distribution": json.dumps(answer.vote_distribution) if answer.vote_distribution else None,
            "match_status": answer.match_status,
            "status": answer.status.value,
            "model_version": answer.model_version,
            "source": answer.source,
            "run_id": answer.run_id,
            "batch_id": answer.batch_id,
        }

        with get_pg_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        logger.info("Inserted suggested answer D%s Q%s", answer.district_id, answer.q_id)


def save_eval_result(result: dict, config: Config) -> None:
    """Write one row to silver.eval_results with all stage diagnostics."""
    with logfire.span(
        "save_eval_result",
        district_id=result.get("district_id"),
        q_id=result.get("q_id"),
        match_status=result.get("match_status"),
    ) if logfire else _nullcontext():
        sql = """
            INSERT INTO silver.eval_results (
                run_id, district_id, ay_id, q_id, q_text, q_ans_type,
                predicted, golden, match_status,
                n_chunks_retrieved, n_answer_bearing, retrieval_recall,
                predictions_by_k,
                entropy, agreement_pct, n_unique_answers, vote_distribution,
                any_k_correct, synthesis_correct, synthesis_helped, synthesis_hurt, first_correct_k,
                n_citations, n_valid_doc_ids, n_with_quotes,
                model_version
            ) VALUES (
                %(run_id)s, %(district_id)s, %(ay_id)s, %(q_id)s, %(q_text)s, %(q_ans_type)s,
                %(predicted)s, %(golden)s, %(match_status)s,
                %(n_chunks_retrieved)s, %(n_answer_bearing)s, %(retrieval_recall)s,
                %(predictions_by_k)s,
                %(entropy)s, %(agreement_pct)s, %(n_unique_answers)s, %(vote_distribution)s,
                %(any_k_correct)s, %(synthesis_correct)s, %(synthesis_helped)s, %(synthesis_hurt)s, %(first_correct_k)s,
                %(n_citations)s, %(n_valid_doc_ids)s, %(n_with_quotes)s,
                %(model_version)s
            )
            ON CONFLICT (run_id, q_id) DO UPDATE SET
                predicted = EXCLUDED.predicted,
                golden = EXCLUDED.golden,
                match_status = EXCLUDED.match_status,
                n_chunks_retrieved = EXCLUDED.n_chunks_retrieved,
                n_answer_bearing = EXCLUDED.n_answer_bearing,
                retrieval_recall = EXCLUDED.retrieval_recall,
                predictions_by_k = EXCLUDED.predictions_by_k,
                entropy = EXCLUDED.entropy,
                agreement_pct = EXCLUDED.agreement_pct,
                n_unique_answers = EXCLUDED.n_unique_answers,
                vote_distribution = EXCLUDED.vote_distribution,
                any_k_correct = EXCLUDED.any_k_correct,
                synthesis_correct = EXCLUDED.synthesis_correct,
                synthesis_helped = EXCLUDED.synthesis_helped,
                synthesis_hurt = EXCLUDED.synthesis_hurt,
                first_correct_k = EXCLUDED.first_correct_k,
                n_citations = EXCLUDED.n_citations,
                n_valid_doc_ids = EXCLUDED.n_valid_doc_ids,
                n_with_quotes = EXCLUDED.n_with_quotes,
                model_version = EXCLUDED.model_version,
                created_at = NOW()
        """
        params = {
            "run_id": result["run_id"],
            "district_id": result["district_id"],
            "ay_id": result["ay_id"],
            "q_id": result["q_id"],
            "q_text": result.get("q_text"),
            "q_ans_type": result.get("q_ans_type"),
            "predicted": result.get("predicted"),
            "golden": result.get("golden"),
            "match_status": result.get("match_status"),
            "n_chunks_retrieved": result.get("n_chunks_retrieved"),
            "n_answer_bearing": result.get("n_answer_bearing"),
            "retrieval_recall": result.get("retrieval_recall"),
            "predictions_by_k": json.dumps(result["predictions_by_k"]) if result.get("predictions_by_k") else None,
            "entropy": result.get("entropy"),
            "agreement_pct": result.get("agreement_pct"),
            "n_unique_answers": result.get("n_unique_answers"),
            "vote_distribution": json.dumps(result["vote_distribution"]) if result.get("vote_distribution") else None,
            "any_k_correct": result.get("any_k_correct"),
            "synthesis_correct": result.get("synthesis_correct"),
            "synthesis_helped": result.get("synthesis_helped"),
            "synthesis_hurt": result.get("synthesis_hurt"),
            "first_correct_k": result.get("first_correct_k"),
            "n_citations": result.get("n_citations"),
            "n_valid_doc_ids": result.get("n_valid_doc_ids"),
            "n_with_quotes": result.get("n_with_quotes"),
            "model_version": result.get("model_version"),
        }

        with get_pg_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        logger.info("Saved eval result D%s Q%s -> %s", result['district_id'], result['q_id'], result.get('match_status'))


def save_evidence(
    suggested: SuggestedAnswer,
    config: Config,
    generation_id: UUID,
    run_id: str,
    chunks: list | None = None,
) -> int:
    """Write validated citations to silver.evidence.

    Each citation becomes one evidence row with a deterministic key
    pp-{district}-{ay}-{qid}-{doc_id}-{hash8} allowing multiple quotes
    per document.
    """
    from prediction_pipeline.validate import make_evidence_id

    citations = suggested.citations_json or []
    if not citations:
        return 0

    with logfire.span(
        "save_evidence",
        district_id=suggested.district_id,
        q_id=suggested.q_id,
        n_citations=len(citations),
    ) if logfire else _nullcontext():
        chunk_lookup: dict[str, object] = {}
        if chunks:
            for c in chunks:
                if hasattr(c, 'doc_id') and c.doc_id not in chunk_lookup:
                    chunk_lookup[c.doc_id] = c

        sql = """
            INSERT INTO silver.evidence (
                evidence_id, district_id, ay_id, q_id, document_id,
                original_quote, corrected_quote, match_type, verified,
                chunk_text, page_number, section_heading,
                relevance_score, source_type, source_ref, created_at
            ) VALUES (
                %(evidence_id)s, %(district_id)s, %(ay_id)s, %(q_id)s, %(document_id)s::uuid,
                %(original_quote)s, %(corrected_quote)s, %(match_type)s, %(verified)s,
                %(chunk_text)s, %(page_number)s, %(section_heading)s,
                %(relevance_score)s, %(source_type)s, %(source_ref)s, NOW()
            )
            ON CONFLICT (evidence_id) DO UPDATE SET
                document_id = EXCLUDED.document_id,
                original_quote = EXCLUDED.original_quote,
                corrected_quote = EXCLUDED.corrected_quote,
                match_type = EXCLUDED.match_type,
                verified = EXCLUDED.verified,
                chunk_text = EXCLUDED.chunk_text,
                page_number = EXCLUDED.page_number,
                section_heading = EXCLUDED.section_heading,
                relevance_score = EXCLUDED.relevance_score,
                source_type = EXCLUDED.source_type,
                source_ref = EXCLUDED.source_ref,
                created_at = NOW()
        """

        id_mapping = resolve_doc_ids(config, suggested.district_id)

        params = []
        for cite in citations:
            doc_id = cite.get("doc_id", "")
            if not _UUID_RE.match(doc_id or ""):
                resolved = resolve_single_doc_id(doc_id, id_mapping)
                if resolved:
                    doc_id = resolved
                else:
                    doc_name = cite.get("doc_name", "")
                    if chunks:
                        for c in chunks:
                            if hasattr(c, 'doc_name') and c.doc_name and c.doc_name.lower() == doc_name.lower():
                                doc_id = c.doc_id
                                break
            if not doc_id or not _UUID_RE.match(doc_id):
                continue

            quote = cite.get("quote", "")
            original_quote = cite.get("original_quote", quote)
            evidence_id = make_evidence_id(
                suggested.district_id, suggested.ay_id, suggested.q_id, doc_id, original_quote,
            )

            chunk = chunk_lookup.get(doc_id)
            chunk_text = None
            if chunk and hasattr(chunk, 'text'):
                chunk_text = chunk.text[:100_000]

            params.append({
                "evidence_id": evidence_id,
                "district_id": suggested.district_id,
                "ay_id": suggested.ay_id,
                "q_id": suggested.q_id,
                "document_id": doc_id,
                "original_quote": original_quote,
                "corrected_quote": quote,
                "match_type": cite.get("match_type"),
                "verified": cite.get("verified"),
                "chunk_text": chunk_text,
                "page_number": cite.get("page_number"),
                "section_heading": cite.get("section_heading"),
                "relevance_score": cite.get("relevance_score"),
                "source_type": "ai_prediction",
                "source_ref": f"gen:{generation_id}|run:{run_id}",
            })

        if not params:
            return 0

        with get_pg_connection(config) as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql, params)
            conn.commit()
        logger.info("Saved %s evidence rows for D%s Q%s", len(params), suggested.district_id, suggested.q_id)
        return len(params)

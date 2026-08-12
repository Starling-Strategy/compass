"""Prediction pipeline entry point.

Nathan's PiedPiper equivalent: PredictionPipeline.run() class method.
We use a functional run_pipeline() approach with argparse CLI.

Usage:
    PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25 --dry-run
    PREDICTOR_DRY_RUN=false PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25
    PREDICTOR_DRY_RUN=false PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25 --q-ids 4,5,6
"""
import argparse
import asyncio
import logging
import time
from contextlib import nullcontext as _nullcontext
from uuid import uuid4

from prediction_pipeline.config import Config
from prediction_pipeline.db import (
    get_district_info,
    load_golden_answers,
    load_prior_year_answers,
    load_questions,
    save_eval_result,
    save_evidence,
    save_prediction_runs,
    insert_suggested_answer,
)
from prediction_pipeline.evaluate import (
    evaluate,
    evaluate_batch,
    evaluate_citations,
    evaluate_predictions_by_k,
    evaluate_retrieval,
    evaluate_synthesis,
)
from prediction_pipeline.evals import build_eval_dataset, build_snapshot, save_snapshot
from prediction_pipeline.observability import setup as setup_observability
from prediction_pipeline.predict import (
    build_prompt,
    classify_question,
    create_question_cache,
    delete_question_cache,
    normalize_multiselect_answer,
    predict_async,
    predict_multiselect_enum,
    resolve_strategy,
    resolve_system_prompt,
    resolve_top_k,
    verify_ina_async,
)
from prediction_pipeline.models import QuestionType
from prediction_pipeline.retriever import build_search_query, get_retriever, PostgresRetriever, VespaRetriever
from prediction_pipeline.synthesize import pick_citations, synthesize
from prediction_pipeline.validate import (
    collect_unique_citations,
    validate_citations_batch,
    patch_reasoning,
)

from prediction_pipeline.models import ResolvedCitation, SuggestedAnswer

try:
    import logfire
except ImportError:
    logfire = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run prediction pipeline")
    parser.add_argument("--district", type=int, required=True, help="District ID")
    parser.add_argument("--ay", type=int, default=25, help="Academic year ID (25 = 2024-2025)")
    parser.add_argument("--q-ids", type=str, default=None, help="Comma-separated question IDs")
    parser.add_argument("--limit", type=int, default=0, help="Max questions (0 = all)")
    parser.add_argument("--priority", type=str, default=None, help="Filter by q_priority (e.g. 'High')")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
    parser.add_argument("--no-dry-run", action="store_true", help="Force live run (overrides PREDICTOR_DRY_RUN)")
    return parser.parse_args()


def annotate_ina_audit(suggested: SuggestedAnswer, prior_answers: dict[int, str],
                       prior_ay: int) -> bool:
    """Append audit note when INA predicted but prior year had a value.

    Returns True if an audit note was added. Does NOT change the answer —
    Katherine's rule: silence = INA, always. This is observation-only.
    """
    if not suggested.is_ina or suggested.q_id not in prior_answers:
        return False
    prior_val = prior_answers[suggested.q_id]
    audit_note = f"[Note: AY{prior_ay} had value '{prior_val}' — flagged for analyst review]"
    suggested.reasoning = (
        f"{suggested.reasoning}\n\n{audit_note}" if suggested.reasoning
        else audit_note
    )
    return True


async def process_question(question, district_id, ay_id, district_name, state,
                           retriever, config, run_id):
    """Process one question: retrieve docs, run parallel predictions, synthesize.

    Uses adaptive per-question-type strategy for k range, temperature,
    INA threshold, and verification gating.
    """
    generation_id = uuid4()
    runs = []
    strategy = resolve_strategy(question, config)

    query = build_search_query(question, ay_id=ay_id)
    effective_k_min = min(strategy.k_min, strategy.k_max)
    all_chunks = retriever.retrieve(district_id, ay_id, query, top_k=strategy.k_max)

    # --- Context caching for k-diversity ---
    cache_name = None
    k_range = range(effective_k_min, strategy.k_max + 1)
    if config.use_caching and len(k_range) > 1:
        try:
            sys_prompt = resolve_system_prompt(question.q_id, question.q_ans_type, ay_id)
            full_prompt = build_prompt(question, district_name, state, all_chunks)
            cache_name = await create_question_cache(sys_prompt, full_prompt, config)
            logger.info("  Cache created for Q%s", question.q_id)
        except Exception as e:
            logger.warning("  Cache creation failed for Q%s, using uncached: %s", question.q_id, e)
            cache_name = None

    tasks = []
    for k in k_range:
        chunks = all_chunks[:k]
        tasks.append(
            predict_async(
                question, district_id, ay_id, district_name, state,
                chunks, config, generation_id, top_k=k, run_id=run_id,
                temperature=strategy.temperature,
                cached_content=cache_name,
            )
        )

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if cache_name:
            await delete_question_cache(cache_name)

    for k, result in zip(k_range, results):
        if isinstance(result, Exception):
            logger.error("  Prediction failed at k=%s: %r", k, result)
            continue
        runs.append(result)

    if not runs:
        logger.warning("  No successful predictions for Q%s", question.q_id)
        return None, runs, all_chunks

    # --- Postgres fallback on retrieval gap ---
    no_evidence_count = sum(
        1 for r in runs if getattr(r, "evidence_agreement", None) == "no_evidence"
    )
    if no_evidence_count == len(runs) and isinstance(retriever, VespaRetriever):
        logger.info("  FALLBACK: Q%s retrieval gap — retrying with PostgresRetriever", question.q_id)
        pg_retriever = PostgresRetriever(config)
        pg_chunks = pg_retriever.retrieve(district_id, ay_id, query, top_k=strategy.k_max)
        if pg_chunks:
            pg_runs = []
            pg_tasks = []
            for k in range(effective_k_min, strategy.k_max + 1):
                chunks = pg_chunks[:k]
                pg_tasks.append(
                    predict_async(
                        question, district_id, ay_id, district_name, state,
                        chunks, config, generation_id, top_k=k, run_id=run_id,
                        temperature=strategy.temperature,
                    )
                )
            pg_results = await asyncio.gather(*pg_tasks, return_exceptions=True)
            for k, result in zip(range(effective_k_min, strategy.k_max + 1), pg_results):
                if isinstance(result, Exception):
                    logger.error("  Fallback prediction failed at k=%s: %r", k, result)
                    continue
                pg_runs.append(result)

            pg_no_evidence = sum(
                1 for r in pg_runs if getattr(r, "evidence_agreement", None) == "no_evidence"
            )
            if pg_runs and pg_no_evidence < len(pg_runs):
                logger.info("  FALLBACK SUCCESS: Q%s — Postgres found evidence (%s/%s runs)",
                            question.q_id, len(pg_runs) - pg_no_evidence, len(pg_runs))
                runs = pg_runs
                all_chunks = pg_chunks

    # --- Synthesize with strategy's INA threshold ---
    suggested = synthesize(generation_id, runs, config, ina_threshold_override=strategy.ina_threshold)

    # --- Strategy-gated INA verification (the critical fix) ---
    should_verify = (
        suggested.is_ina
        and strategy.question_type != QuestionType.DATE
        and (strategy.always_verify_ina or suggested.entropy > 0.5)
    )
    if should_verify:
        verify_result = await verify_ina_async(
            question, district_name, state, all_chunks[:11], config,
        )
        if verify_result:
            val, conf, reason = verify_result
            suggested.suggested_answer = val
            suggested.is_ina = False
            suggested.confidence = conf
            suggested.reasoning = reason
            suggested.citations_json = pick_citations(runs, val)
            logger.info("  INA override by verification: Q%s -> '%s'", question.q_id, val)

    # --- Multi-select enumeration for MULTISELECT questions ---
    if strategy.question_type == QuestionType.MULTISELECT:
        enum_result = await predict_multiselect_enum(
            question, district_name, state, all_chunks[:strategy.k_max], config,
        )
        if enum_result:
            if suggested.is_ina:
                # INA -> replace with enum result only if citations exist
                enum_citations = pick_citations(runs, enum_result)
                if enum_citations:
                    suggested.suggested_answer = enum_result
                    suggested.is_ina = False
                    suggested.confidence = 0.7
                    suggested.citations_json = enum_citations
                    logger.info("  Multiselect enum replaced INA: Q%s -> '%s'", question.q_id, enum_result)
                else:
                    logger.info("  Multiselect enum skipped (no citations): Q%s would be '%s' but keeping INA", question.q_id, enum_result)
            else:
                # Merge: union of synthesis options + enum options
                existing = {o.strip() for o in suggested.suggested_answer.split(",")}
                enum_opts = {o.strip() for o in enum_result.split(",")}
                merged = existing | enum_opts
                if merged != existing:
                    suggested.suggested_answer = ", ".join(sorted(merged))
                    logger.info("  Multiselect enum merged: Q%s -> '%s'", question.q_id, suggested.suggested_answer)

    # --- Multi-select "Other" detection ---
    if (strategy.question_type == QuestionType.MULTISELECT
            and not suggested.is_ina
            and question.valid_options):
        normalized = normalize_multiselect_answer(suggested.suggested_answer, question.valid_options)
        if normalized != suggested.suggested_answer:
            logger.info("  Multiselect Other appended: Q%s '%s' -> '%s'",
                        question.q_id, suggested.suggested_answer, normalized)
            suggested.suggested_answer = normalized

    # --- Validate only the suggested answer's citations (post-synthesis) ---
    if suggested.citations_json:
        try:
            # Build ResolvedCitation objects from the suggested answer's picks
            to_validate = []
            for cite_dict in suggested.citations_json:
                try:
                    to_validate.append(ResolvedCitation(**cite_dict))
                except Exception:
                    pass

            if to_validate:
                unique_pairs = collect_unique_citations(to_validate, all_chunks)
                if unique_pairs:
                    curated_list = await validate_citations_batch(unique_pairs, config)
                    curated_lookup = {
                        (c.doc_id, c.original_quote): c for c in curated_list
                    }

                    # Update citations_json with validation results;
                    # keep unverified citations with verified=false flag
                    curated_by_idx = {}
                    for cite_dict in suggested.citations_json:
                        key = (cite_dict.get("doc_id", ""), cite_dict.get("quote", ""))
                        curated = curated_lookup.get(key)
                        if curated:
                            cite_dict["verified"] = curated.verified
                            cite_dict["match_type"] = curated.match_type
                            cite_dict["original_quote"] = curated.original_quote
                            if curated.verified:
                                cite_dict["quote"] = curated.corrected_quote
                            idx = cite_dict.get("document_index")
                            if idx:
                                curated_by_idx[idx] = curated

                    # Patch reasoning with validated quotes
                    if suggested.reasoning and curated_by_idx:
                        suggested.reasoning = patch_reasoning(
                            suggested.reasoning, curated_by_idx
                        )
        except Exception:
            logger.exception("  Citation validation failed (non-fatal) for Q%s", question.q_id)

    return suggested, runs, all_chunks


async def run_pipeline(config: Config, args: argparse.Namespace):
    """Main pipeline flow."""
    start = time.time()
    run_id = f"run_{int(start)}"

    # 1. Load questions
    q_ids = [int(x) for x in args.q_ids.split(",")] if args.q_ids else None
    priority = getattr(args, "priority", None)
    questions = load_questions(config, q_ids=q_ids, priority=priority)
    if config.limit > 0:
        questions = questions[:config.limit]
    logger.info("Loaded %s questions", len(questions))

    if not questions:
        logger.info("No questions to process.")
        return

    # 2. District info
    district_name, state = get_district_info(config, config.district_id)
    logger.info("District: %s, %s (ID=%s)", district_name, state, config.district_id)

    # 3. Retriever
    retriever = get_retriever(config)

    # 4. Golden answers + prior-year answers for INA audit trail
    golden = load_golden_answers(config, config.district_id, config.ay_id)
    logger.info("Loaded %s golden answers", len(golden))
    prior_ay = config.ay_id - 1
    prior_answers = load_prior_year_answers(config, config.district_id, prior_ay)
    if prior_answers:
        logger.info("Loaded %s prior-year answers (AY%s) for INA audit", len(prior_answers), prior_ay)

    if config.dry_run:
        logger.info("DRY RUN — showing what would be processed:")
        for q in questions[:20]:
            golden_val = golden.get(q.q_id, "—")
            print(f"  Q{q.q_id:3d} | {q.q_ans_type:30s} | golden={golden_val:10s} | {q.q_text[:60]}")
        if len(questions) > 20:
            print(f"  ... and {len(questions) - 20} more")
        print(f"\nSet PREDICTOR_DRY_RUN=false to run predictions.")
        return

    # 5. Process questions with parallelism
    all_suggested = []
    all_runs = []
    q_map = {q.q_id: q for q in questions}
    completed = 0

    loop = asyncio.get_event_loop()

    async def _handle_question(i, question):
        nonlocal completed
        logger.info("[%s/%s] Q%s: %s", i, len(questions), question.q_id, question.q_text[:60])

        with logfire.span(
            "process_question", q_id=question.q_id,
            district_id=config.district_id, district_name=district_name,
            model_version=config.model_version,
        ) if logfire else _nullcontext():
            suggested, runs, chunks = await process_question(
                question, config.district_id, config.ay_id,
                district_name, state, retriever, config, run_id,
            )

        if not suggested:
            return

        no_evidence_count = sum(
            1 for r in runs if getattr(r, "evidence_agreement", None) == "no_evidence"
        )
        if no_evidence_count == len(runs):
            logger.warning(
                "  RETRIEVAL GAP: Q%s — all %s predictions reported no_evidence (D%s)",
                question.q_id, len(runs), config.district_id,
            )


        if annotate_ina_audit(suggested, prior_answers, prior_ay):
            logger.info("  INA audit: Q%s was '%s' in AY%s", question.q_id, prior_answers[question.q_id], prior_ay)

        await loop.run_in_executor(None, save_prediction_runs, runs, config)
        all_runs.extend(runs)

        match_status = evaluate(suggested, golden, q_ans_type=question.q_ans_type)
        suggested.match_status = match_status.value
        suggested.run_id = run_id

        await loop.run_in_executor(None, insert_suggested_answer, suggested, config)
        try:
            await loop.run_in_executor(
                None, save_evidence, suggested, config, suggested.generation_id, run_id, chunks,
            )
        except Exception:
            logger.exception("  save_evidence failed (non-fatal)")

        golden_val = golden.get(question.q_id)
        retrieval_eval = evaluate_retrieval(chunks, golden_val, question.q_ans_type)
        preds_by_k = evaluate_predictions_by_k(runs, golden_val, question.q_ans_type)
        synth_eval = evaluate_synthesis(preds_by_k, suggested.suggested_answer, golden_val, question.q_ans_type)
        cite_eval = evaluate_citations(suggested)

        eval_row = {
            "run_id": run_id,
            "district_id": config.district_id,
            "ay_id": config.ay_id,
            "q_id": question.q_id,
            "q_text": question.q_text,
            "q_ans_type": question.q_ans_type,
            "predicted": suggested.suggested_answer,
            "golden": golden_val,
            "match_status": match_status.value,
            **retrieval_eval,
            "predictions_by_k": preds_by_k,
            "entropy": suggested.entropy,
            "agreement_pct": suggested.agreement_pct,
            "n_unique_answers": suggested.n_unique_answers,
            "vote_distribution": suggested.vote_distribution,
            **synth_eval,
            **cite_eval,
            "model_version": config.model_version,
        }
        try:
            await loop.run_in_executor(None, save_eval_result, eval_row, config)
        except Exception:
            logger.exception("  save_eval_result failed (non-fatal)")


        all_suggested.append(suggested)
        completed += 1

        logger.info(
            "  -> %s (conf=%.0f%%, entropy=%.2f, match=%s) [%s/%s]",
            suggested.suggested_answer, suggested.confidence * 100,
            suggested.entropy, match_status.value, completed, len(questions),
        )

    # Throttle concurrency to stay under Gemini rate limits
    semaphore = asyncio.Semaphore(config.question_concurrency)
    logger.info("Processing %s questions (concurrency=%s)", len(questions), config.question_concurrency)

    async def _throttled(i, q):
        async with semaphore:
            return await _handle_question(i, q)

    tasks = [_throttled(i, q) for i, q in enumerate(questions, 1)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error("Q%s failed: %s: %s", questions[i].q_id, type(result).__name__, result)

    # 6. Evaluation summary
    if all_suggested:
        eval_summary = evaluate_batch(all_suggested, golden, questions=q_map)

        logger.info("=" * 70)
        logger.info("EVALUATION SUMMARY")
        logger.info("=" * 70)
        logger.info("  Total:              %s", eval_summary['total'])
        logger.info("  Accuracy:           %.1f%%", eval_summary['accuracy'])
        logger.info("  EXACT:              %s", eval_summary['EXACT'])
        logger.info("  INA_CORRECT:        %s", eval_summary['INA_CORRECT'])
        logger.info("  INA_FALSE_POS:      %s", eval_summary['INA_FALSE_POS'])
        logger.info("  INA_FALSE_NEG:      %s (Katherine violations)", eval_summary['INA_FALSE_NEG'])
        logger.info("  VALUE_DIFFERENT:    %s", eval_summary['VALUE_DIFFERENT'])
        logger.info("  NOGOLDEN:           %s", eval_summary['NOGOLDEN'])
        if eval_summary.get("ina_precision") is not None:
            logger.info("  INA precision:      %.2f%%", eval_summary['ina_precision'] * 100)
        if eval_summary.get("ina_recall") is not None:
            logger.info("  INA recall:         %.2f%%", eval_summary['ina_recall'] * 100)
        logger.info("=" * 70)

        # Build and save snapshot
        if config.save_snapshots:
            snapshot = build_snapshot(
                all_suggested, golden, q_map, config, district_name, eval_summary,
            )
            save_snapshot(snapshot, config)

        # Build pydantic-evals dataset and print
        dataset = build_eval_dataset(all_suggested, golden, q_map)
        logger.info("Eval dataset: %s cases", len(dataset.cases))

    # Cost summary
    try:
        from prediction_pipeline.cost_tracker import get_tracker
        tracker = get_tracker(config.prediction_model)
        if tracker.call_count > 0:
            logger.info("COST: %s", tracker.summary())
    except Exception:
        pass

    elapsed = time.time() - start
    logger.info("Pipeline complete in %.0fs (%s answers, %s predictions)", elapsed, len(all_suggested), len(all_runs))


def main():
    setup_observability("predictor")
    args = parse_args()
    config = Config()

    # CLI overrides
    config.district_id = args.district
    config.ay_id = args.ay
    if args.limit:
        config.limit = args.limit
    if args.dry_run:
        config.dry_run = True
    if args.no_dry_run:
        config.dry_run = False

    # Run
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run_pipeline(config, args))
    finally:
        loop.close()


if __name__ == "__main__":
    main()

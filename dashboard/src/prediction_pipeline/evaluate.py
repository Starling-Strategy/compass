"""Evaluate predictions: 6-way INA-aware accuracy + stage diagnostics.

PiedPiper equivalent: evaluate_predictions()
Key addition: type-aware comparison (numeric tolerance, sorted multi-select).

Stage evaluation functions (added for Pipeline Evaluation Dashboard):
- evaluate_retrieval: Did Vespa return the right documents?
- evaluate_predictions_by_k: How did each k perform?
- evaluate_synthesis: Did voting help or hurt?
- evaluate_citations: Are citations real and useful?
"""
import re
from contextlib import nullcontext as _nullcontext

from prediction_pipeline.models import Chunk, INA_VARIANTS, MatchStatus, PredictionRun, SuggestedAnswer

try:
    import logfire
except ImportError:
    logfire = None

_INA_NORMALIZED = INA_VARIANTS

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)



def normalize_answer(answer: str) -> str:
    answer = answer.strip().lower()
    if answer in _INA_NORMALIZED:
        return "ina"
    if "$" in answer:
        answer = re.sub(r"[$,]", "", answer)
    if answer.endswith("%"):
        answer = answer[:-1].strip()
    if "," in answer:
        parts = [p.strip() for p in answer.split(",")]
        answer = ", ".join(sorted(parts))
    return answer


def is_ina(answer: str) -> bool:
    return normalize_answer(answer) == "ina"


def _try_numeric(s: str) -> float | None:
    """Try to parse a normalized answer as a number."""
    try:
        return float(s)
    except ValueError:
        return None


def _numeric_match(pred: str, gold: str, tolerance: float = 0.005) -> bool:
    """True if pred and gold are numerically within tolerance (0.5% default)."""
    p, g = _try_numeric(pred), _try_numeric(gold)
    if p is None or g is None:
        return False
    if g == 0 and p == 0:
        return True
    return abs(p - g) / max(abs(g), 1) < tolerance


def _multiselect_match(pred: str, gold: str) -> bool:
    """True if pred and gold contain the same set of options (order-independent).

    Note: normalize_answer() already sorts comma-separated values, so this is
    a safety net for cases where normalization doesn't fully resolve ordering
    (e.g., "option a, option b" vs "option b, option a" after normalization).
    """
    pred_set = {p.strip() for p in pred.split(",")}
    gold_set = {g.strip() for g in gold.split(",")}
    return pred_set == gold_set


def _matches(pred: str, gold: str, q_ans_type: str | None = None) -> bool:
    """Shared match logic: INA-aware, type-aware comparison.

    Returns True if pred matches gold (both already normalized).
    """
    pred_ina = pred == "ina"
    gold_ina = gold == "ina"
    if pred_ina and gold_ina:
        return True
    if pred_ina != gold_ina:
        return False
    if pred == gold:
        return True
    if q_ans_type:
        ans_lower = q_ans_type.lower()
        if "numeric" in ans_lower or "number" in ans_lower or "currency" in ans_lower:
            return _numeric_match(pred, gold)
        elif "checkbox" in ans_lower or "multi" in ans_lower:
            return _multiselect_match(pred, gold)
    return False


def evaluate(suggested: SuggestedAnswer, golden_answers: dict[int, str],
             q_ans_type: str | None = None) -> MatchStatus:
    """6-way comparison with optional type-aware matching.

    When q_ans_type is provided:
    - Numeric types: 0.5% tolerance (e.g., $94,444 vs $94,500 -> EXACT)
    - Multi-select types: order-independent set comparison
    - Other types: exact string match (current behavior)
    """
    with logfire.span(
        "evaluate",
        q_id=suggested.q_id,
        q_ans_type=q_ans_type,
    ) if logfire else _nullcontext():
        golden = golden_answers.get(suggested.q_id)
        if golden is None:
            return MatchStatus.NOGOLDEN
        norm_pred = normalize_answer(suggested.suggested_answer)
        norm_gold = normalize_answer(golden)
        pred_ina = norm_pred == "ina"
        gold_ina = norm_gold == "ina"
        if pred_ina and gold_ina:
            status = MatchStatus.INA_CORRECT
        elif pred_ina and not gold_ina:
            status = MatchStatus.INA_FALSE_POS
        elif not pred_ina and gold_ina:
            status = MatchStatus.INA_FALSE_NEG
        elif _matches(norm_pred, norm_gold, q_ans_type):
            status = MatchStatus.EXACT
        else:
            status = MatchStatus.VALUE_DIFFERENT

        if logfire:
            logfire.info("eval_result", match_status=status.value, predicted=norm_pred, golden=norm_gold)
        return status


def _answer_in_text(answer: str, text: str) -> bool:
    """Check if a golden answer appears in chunk text (case-insensitive keyword search)."""
    norm = normalize_answer(answer)
    if is_ina(answer):
        return False  # INA can't be "found" in text
    text_lower = text.lower()
    # Direct substring match
    if norm in text_lower:
        return True
    # Try original (un-normalized) answer too
    if answer.strip().lower() in text_lower:
        return True
    return False


def evaluate_retrieval(
    chunks: list[Chunk],
    golden_answer: str | None,
    q_ans_type: str | None = None,
) -> dict:
    """Stage 1: Did retrieval find documents containing the golden answer?

    Returns dict with n_chunks_retrieved, n_answer_bearing, retrieval_recall.
    retrieval_recall is None if golden is INA or missing (can't measure).
    """
    n_chunks = len(chunks)
    if golden_answer is None or is_ina(golden_answer):
        return {
            "n_chunks_retrieved": n_chunks,
            "n_answer_bearing": None,
            "retrieval_recall": None,
        }
    n_bearing = sum(1 for c in chunks if _answer_in_text(golden_answer, c.text))
    recall = n_bearing / n_chunks if n_chunks > 0 else 0.0
    return {
        "n_chunks_retrieved": n_chunks,
        "n_answer_bearing": n_bearing,
        "retrieval_recall": round(recall, 4),
    }


def evaluate_predictions_by_k(
    runs: list[PredictionRun],
    golden_answer: str | None,
    q_ans_type: str | None = None,
) -> list[dict]:
    """Stage 2: For each of the 11 PredictionRuns, did it match golden?

    Returns list of {k, answer, correct} dicts sorted by k.
    """
    if golden_answer is None:
        return [
            {"k": r.top_k, "answer": r.predicted_answer, "correct": None}
            for r in sorted(runs, key=lambda r: r.top_k)
        ]

    norm_gold = normalize_answer(golden_answer)
    results = []
    for run in sorted(runs, key=lambda r: r.top_k):
        norm_pred = normalize_answer(run.predicted_answer)
        correct = _matches(norm_pred, norm_gold, q_ans_type)
        results.append({"k": run.top_k, "answer": run.predicted_answer, "correct": correct})
    return results


def evaluate_synthesis(
    predictions_by_k: list[dict],
    final_answer: str,
    golden_answer: str | None,
    q_ans_type: str | None = None,
) -> dict:
    """Stage 3: Did voting help or hurt?

    Returns dict with any_k_correct, synthesis_correct, synthesis_helped,
    synthesis_hurt, first_correct_k.
    """
    if golden_answer is None:
        return {
            "any_k_correct": None,
            "synthesis_correct": None,
            "synthesis_helped": None,
            "synthesis_hurt": None,
            "first_correct_k": None,
        }

    norm_final = normalize_answer(final_answer)
    norm_gold = normalize_answer(golden_answer)
    synthesis_correct = _matches(norm_final, norm_gold, q_ans_type)

    # Analyze individual k results
    correct_ks = [p for p in predictions_by_k if p.get("correct") is True]
    incorrect_ks = [p for p in predictions_by_k if p.get("correct") is False]
    any_k_correct = len(correct_ks) > 0
    first_correct_k = min((p["k"] for p in correct_ks), default=None)

    # synthesis_helped: final correct when <50% of k's were correct
    n_evaluated = len(correct_ks) + len(incorrect_ks)
    pct_correct = len(correct_ks) / n_evaluated if n_evaluated > 0 else 0
    synthesis_helped = synthesis_correct and pct_correct < 0.5
    synthesis_hurt = not synthesis_correct and pct_correct > 0.5

    return {
        "any_k_correct": any_k_correct,
        "synthesis_correct": synthesis_correct,
        "synthesis_helped": synthesis_helped,
        "synthesis_hurt": synthesis_hurt,
        "first_correct_k": first_correct_k,
    }


def evaluate_citations(suggested: SuggestedAnswer) -> dict:
    """Stage 4: Are citations real and useful?

    Returns dict with n_citations, n_valid_doc_ids, n_with_quotes.
    """
    citations = suggested.citations_json or []
    n_citations = len(citations)
    n_valid = sum(1 for c in citations if _UUID_RE.match(c.get("doc_id", "")))
    n_quotes = sum(1 for c in citations if c.get("quote", "").strip())
    return {
        "n_citations": n_citations,
        "n_valid_doc_ids": n_valid,
        "n_with_quotes": n_quotes,
    }


def evaluate_batch(answers: list[SuggestedAnswer], golden: dict[int, str],
                   questions: dict[int, object] | None = None) -> dict:
    """Evaluate a batch of answers. Pass questions dict for type-aware matching."""
    with logfire.span(
        "evaluate_batch",
        n_answers=len(answers),
    ) if logfire else _nullcontext():
        counts = {s.value: 0 for s in MatchStatus}
        for a in answers:
            q_ans_type = None
            if questions and a.q_id in questions:
                q_ans_type = getattr(questions[a.q_id], "q_ans_type", None)
            status = evaluate(a, golden, q_ans_type=q_ans_type)
            a.match_status = status.value
            counts[status.value] += 1
        total = len(answers)
        evaluated = total - counts["NOGOLDEN"]
        correct = counts["EXACT"] + counts["INA_CORRECT"]
        ina_tp = counts["INA_CORRECT"]
        ina_fp = counts["INA_FALSE_POS"]
        ina_fn = counts["INA_FALSE_NEG"]
        summary = {
            "total": total, **counts,
            "accuracy": round(correct / evaluated * 100, 1) if evaluated else 0.0,
            "ina_precision": round(ina_tp / (ina_tp + ina_fp), 4) if (ina_tp + ina_fp) else None,
            "ina_recall": round(ina_tp / (ina_tp + ina_fn), 4) if (ina_tp + ina_fn) else None,
            "silence_rule_violations": ina_fn,
        }

        if logfire:
            logfire.info(
                "eval_batch_summary",
                accuracy=summary["accuracy"],
                silence_rule_violations=ina_fn,
                total=total,
            )

        return summary

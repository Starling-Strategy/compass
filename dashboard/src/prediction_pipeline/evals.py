"""pydantic-evals integration for structured evaluation and regression testing.

No direct Nathan PiedPiper equivalent — this is our addition.
Provides snapshot save/load/compare for regression testing across runs.
"""
import json
import logging
import os
from datetime import datetime

from pydantic_evals import Case, Dataset

from prediction_pipeline.config import Config
from prediction_pipeline.models import (
    EvaluationSnapshot,
    QuestionContext,
    QuestionResult,
    SuggestedAnswer,
)

logger = logging.getLogger(__name__)


def build_eval_dataset(
    suggested_answers: list[SuggestedAnswer],
    golden_answers: dict[int, str],
    questions: dict[int, QuestionContext],
) -> Dataset:
    """Build a pydantic-evals Dataset from prediction results."""
    cases = []
    for sa in suggested_answers:
        q = questions.get(sa.q_id)
        golden = golden_answers.get(sa.q_id)
        cases.append(Case(
            name=f"Q{sa.q_id}",
            inputs={
                "district_id": sa.district_id,
                "q_id": sa.q_id,
                "ay_id": sa.ay_id,
                "predicted": sa.suggested_answer,
            },
            expected_output=golden,
            metadata={
                "question_text": q.q_text if q else "",
                "answer_type": q.q_ans_type if q else "",
                "entropy": sa.entropy,
                "agreement_pct": sa.agreement_pct,
                "n_unique_answers": sa.n_unique_answers,
                "vote_distribution": sa.vote_distribution,
                "match_status": sa.match_status,
                "is_ina": sa.is_ina,
            },
        ))
    return Dataset(cases=cases)


def build_snapshot(
    suggested_answers: list[SuggestedAnswer],
    golden_answers: dict[int, str],
    questions: dict[int, QuestionContext],
    config: Config,
    district_name: str,
    eval_summary: dict,
) -> EvaluationSnapshot:
    """Build an EvaluationSnapshot from pipeline results."""
    results = []
    for sa in suggested_answers:
        q = questions.get(sa.q_id)
        results.append(QuestionResult(
            q_id=sa.q_id,
            q_text=q.q_text if q else "",
            q_ans_type=q.q_ans_type if q else "",
            predicted=sa.suggested_answer,
            golden=golden_answers.get(sa.q_id),
            match_status=sa.match_status or "UNKNOWN",
            entropy=sa.entropy,
            agreement_pct=sa.agreement_pct,
            n_unique_answers=sa.n_unique_answers,
            vote_distribution=sa.vote_distribution or {},
            confidence=sa.confidence,
            reasoning=sa.reasoning,
        ))

    return EvaluationSnapshot(
        district_id=config.district_id or 0,
        district_name=district_name,
        ay_id=config.ay_id,
        model_version=config.model_version,
        prediction_model=config.prediction_model,
        timestamp=datetime.now(),
        k_range=(config.k_min, config.k_max),
        ina_threshold=config.ina_threshold,
        results=results,
        summary=eval_summary,
    )


def save_snapshot(snapshot: EvaluationSnapshot, config: Config) -> str:
    """Save evaluation snapshot to disk as JSON. Returns file path."""
    os.makedirs(config.snapshot_dir, exist_ok=True)
    timestamp = snapshot.timestamp.strftime("%Y%m%d_%H%M%S")
    filename = f"D{snapshot.district_id}_AY{snapshot.ay_id}_{timestamp}.json"
    path = os.path.join(config.snapshot_dir, filename)

    with open(path, "w") as f:
        f.write(snapshot.model_dump_json(indent=2))

    logger.info(f"Saved evaluation snapshot to {path}")
    return path


def load_snapshot(path: str) -> EvaluationSnapshot:
    """Load a previous snapshot for regression comparison."""
    with open(path) as f:
        data = json.load(f)
    return EvaluationSnapshot(**data)


def compare_snapshots(
    current: EvaluationSnapshot, baseline: EvaluationSnapshot,
) -> dict:
    """Compare two snapshots. Returns regressions and improvements."""
    baseline_map = {r.q_id: r.match_status for r in baseline.results}
    regressions = []
    improvements = []

    for r in current.results:
        old_status = baseline_map.get(r.q_id)
        if old_status is None:
            continue
        if old_status == "EXACT" and r.match_status != "EXACT":
            regressions.append({"q_id": r.q_id, "was": old_status, "now": r.match_status})
        elif old_status != "EXACT" and r.match_status == "EXACT":
            improvements.append({"q_id": r.q_id, "was": old_status, "now": r.match_status})

    return {
        "regressions": regressions,
        "improvements": improvements,
        "accuracy_delta": (
            current.summary.get("accuracy", 0) - baseline.summary.get("accuracy", 0)
        ),
    }

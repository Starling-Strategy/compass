"""Pydantic models for the Metric Calculator section of the NCTQ.ai dashboard.

Review workflow models: districts, policies, questions, and question review data.
"""

from pydantic import computed_field

from nctqai.models.docs import DashboardModel


class DistrictWithReviewStats(DashboardModel):
    """District row with review progress counts for /mc/districts."""

    district_id: int
    district_name: str
    state: str | None = None
    is_priority: bool = False
    accepted: int = 0
    rejected: int = 0
    unreviewed: int = 0

    @computed_field
    @property
    def total(self) -> int:
        return self.accepted + self.rejected + self.unreviewed

    @computed_field
    @property
    def review_pct(self) -> float:
        return (
            round((self.accepted + self.rejected) / self.total * 100, 1)
            if self.total
            else 0
        )


class PolicyProgress(DashboardModel):
    """Policy row with review counts for /mc/districts/{id}."""

    dpol_id: int
    policy_name: str
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    unreviewed: int = 0


class AnswerHold(DashboardModel):
    """Active hold on a suggested answer preventing analyst review."""

    district_id: int
    ay_id: int
    q_id: int
    held_by: str
    hold_reason: str
    held_at: str | None = None


class QuestionRow(DashboardModel):
    """Single question row within a subpolicy group."""

    q_id: int
    q_num: str | None = None
    q_text: str
    suggested_answer: str | None = None
    status: str = "unreviewed"
    agreement_pct: float | None = None
    n_predictions: int | None = None
    is_ina: bool = False
    is_on_hold: bool = False


class SubpolicyGroup(DashboardModel):
    """A group of questions under a subpolicy heading."""

    subpolicy_name: str
    questions: list[QuestionRow] = []


class QuestionReviewData(DashboardModel):
    """Full context for the question review page /mc/districts/{id}/questions/{qid}."""

    # Context
    district_id: int
    ay_id: int
    q_id: int
    q_num: str | None = None
    district_name: str
    state: str | None = None
    policy_name: str | None = None
    subpolicy_name: str | None = None
    dpol_id: int | None = None
    # Question
    q_text: str
    q_ans_type: str | None = None
    valid_options: list | None = None
    # AI Answer
    suggested_answer: str
    confidence: float = 0
    reasoning: str | None = None
    is_ina: bool = False
    citations_json: dict | list | None = None
    # Voting metrics
    entropy: float = 0
    agreement_pct: float = 0
    n_unique_answers: int = 0
    n_predictions: int = 0
    # Analyst notes
    footnote: str | None = None
    page_ref: str | None = None
    # Review state
    status: str = "unreviewed"
    rejection_reason: str | None = None
    decision_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    predicted_at: str | None = None
    run_id: str | None = None
    # Hold
    hold: AnswerHold | None = None
    # Nav
    prev_q_id: int | None = None
    next_q_id: int | None = None
    position: int | None = None
    total_in_policy: int | None = None

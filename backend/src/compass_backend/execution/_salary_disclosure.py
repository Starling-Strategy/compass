"""Bachelor-starting-salary metric classifier.

Governed disclosure copy and phrase authority live in catalog metadata. This
module only retains the metric-name classifier used by tests and guardrails.
The bachelor-vs-master token logic is shared with the catalog resolver via
``catalog.degree_lane.classify_metric_lane`` — see audit finding #17.
"""

from __future__ import annotations

from compass_backend.catalog.degree_lane import classify_metric_lane

from ._text_utils import metric_phrase_key


def is_bachelor_starting_salary_metric(metric_name: str) -> bool:
    """Return True for a starting-salary metric scoped to the bachelor's lane.

    Substring matching on ``"bachelor"`` used to live here; lane membership
    is now delegated to ``classify_metric_lane``, which inspects whole tokens
    after non-alphanumeric splitting. The two shape checks (``salary`` and
    ``first year teacher`` are substrings of the cleaned phrase key) keep
    their original substring contract because they target multi-token spans,
    not single tokens with ambiguous-lane neighbors.
    """

    key = metric_phrase_key(metric_name)
    return (
        "salary" in key
        and "first year teacher" in key
        and classify_metric_lane(key) == "ba"
    )

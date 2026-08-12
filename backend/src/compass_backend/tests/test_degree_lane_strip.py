"""Fix A (#1 refusal family): degree-lane strip must be BA/MA symmetric.

The resolver recovers a qualifier-extended metric phrase ("starting salary BA
degree" — the planner folds "BA degree" into the metric) by stripping the degree
tokens and re-searching the lane-neutral phrase. The trap the advisor flagged:
``master's`` splits on the apostrophe to ``[master, s]``, and ``db/catalog.py``'s
stopword set is missing ``master``/``masters`` — so a strip built on stopwords
would silently leave the MA lane broken. This pins the symmetry + apostrophe
handling at the unit level (the full 89/96 resolution is validated full-stack).
"""

from __future__ import annotations

from compass_backend.catalog.degree_lane import (
    classify_metric_lane,
    strip_degree_tokens,
)


def test_strip_degree_tokens_ba_and_ma_symmetric() -> None:
    for phrase in (
        "starting salary BA degree",
        "starting salary MA degree",
        "starting salary bachelor's degree",
        "starting salary master's degree",
    ):
        assert strip_degree_tokens(phrase) == "starting salary", phrase


def test_classify_and_strip_agree_across_phrasings() -> None:
    assert classify_metric_lane("starting salary BA degree") == "ba"
    assert classify_metric_lane("starting salary bachelor's degree") == "ba"
    assert classify_metric_lane("starting salary MA degree") == "ma"
    assert classify_metric_lane("starting salary master's degree") == "ma"


def test_strip_degree_tokens_whole_token_only() -> None:
    # "bachelorette" is not a lane token; a phrase with no degree qualifier is
    # returned unchanged (casefolded, whitespace-collapsed).
    assert strip_degree_tokens("bachelorette planning time") == "bachelorette planning time"
    assert strip_degree_tokens("teacher planning time") == "teacher planning time"

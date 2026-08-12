"""Prompt/prose asset loading guardrails for Compass."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from compass_backend.agents.criterion_classifier import (
    _INSTRUCTIONS as CRITERION_CLASSIFIER_INSTRUCTIONS,
)
from compass_backend.catalog.adjudication import CATALOG_ADJUDICATOR_INSTRUCTIONS
from compass_backend.catalog.toolset import CATALOG_TOOLSET_INSTRUCTIONS
from compass_backend.planning.planner import PLANNER_INSTRUCTIONS
from compass_backend.quality.criteria import JUDGE_INSTRUCTIONS


ROOT = Path(__file__).resolve().parents[1]

MODEL_INSTRUCTION_PROMPTS = {
    "planner.md": "You produce one typed PlannerTurn",
    "catalog_adjudicator.md": "You adjudicate one user phrase",
    "catalog_toolset.md": "You use the Compass catalog tools",
    "criterion_classifier.md": "You produce a list of criterion codes",
    "judge.md": "You produce one verdict label",
    "clarify_stylist.md": "You compose one question for a Compass user",
}

SNIPPET_PROMPTS = {
    "coverage-state-language.md": "Coverage-state language",
    "follow-up-reference.md": "Follow-up reference",
    "peer-policy-comparison.md": "Peer policy comparison",
    "peer-salary-comparison.md": "Peer salary comparison",
    "policy-guidance-followups.md": "Policy guidance follow-ups",
    "profile-sort-salary-display.md": "Profile-sort salary display",
    "ranking-and-sorting.md": "Ranking and sorting",
    "similarity-discovery.md": "Similarity / peer-set discovery",
    "teacher-compensation-salary.md": "Teacher compensation salary",
}


def test_prompt_loader_reads_model_instruction_and_planner_guidance_assets() -> None:
    from compass_backend.instructions.loader import (
        load_model_instructions,
        load_planner_guidance,
    )

    for filename, expected_text in MODEL_INSTRUCTION_PROMPTS.items():
        text = load_model_instructions(filename)
        assert expected_text in text
        assert text == text.strip()

    for filename, expected_text in SNIPPET_PROMPTS.items():
        text = load_planner_guidance(filename)
        assert expected_text in text
        assert text == text.strip()


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "../planner.md",
        "model_instructions/planner.md",
        "planner",
        "planner.py",
        "planner.md.bak",
    ],
)
def test_prompt_loader_rejects_non_asset_filenames(filename: str) -> None:
    from compass_backend.instructions.loader import load_model_instructions

    with pytest.raises(ValueError, match="Prompt asset filenames"):
        load_model_instructions(filename)


def test_instruction_constants_remain_import_compatible() -> None:
    assert PLANNER_INSTRUCTIONS.startswith("<role>")
    assert "route=\"policy_guidance\"" in PLANNER_INSTRUCTIONS
    assert "candidate IDs provided in this run" in CATALOG_ADJUDICATOR_INSTRUCTIONS
    assert "Compass catalog tools" in CATALOG_TOOLSET_INSTRUCTIONS
    assert "criterion codes" in CRITERION_CLASSIFIER_INSTRUCTIONS
    assert "JudgmentResult" in JUDGE_INSTRUCTIONS


def test_prompt_assets_are_not_loaded_from_old_planning_snippet_package() -> None:
    stale_package_name = "instruction_snippet" + "_bodies"
    stale_refs = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.py")
        if path.name != "test_prompt_assets.py"
        and stale_package_name in path.read_text(encoding="utf-8")
    ]

    assert stale_refs == []


def test_catalog_adjudicator_includes_conventional_defaults_block() -> None:
    from compass_backend.instructions.loader import load_model_instructions

    text = load_model_instructions("catalog_adjudicator.md")
    assert "<conventional_defaults>" in text
    assert "select_with_alternates" in text
    assert "first-year, bachelor's" in text.lower()
    assert text.index("<contract>") < text.index("<conventional_defaults>") < text.index("<hard_rules>")


def test_static_model_instructions_do_not_live_in_long_python_literals() -> None:
    prompt_constant_names = {
        "PLANNER_INSTRUCTIONS",
        "CATALOG_ADJUDICATOR_INSTRUCTIONS",
        "CATALOG_TOOLSET_INSTRUCTIONS",
        "JUDGE_INSTRUCTIONS",
        "_INSTRUCTIONS",
    }
    offenders: list[str] = []

    for path in [
        ROOT / "planning" / "planner.py",
        ROOT / "catalog" / "adjudication.py",
        ROOT / "catalog" / "toolset.py",
        ROOT / "agents" / "criterion_classifier.py",
        ROOT / "quality" / "criteria.py",
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id not in prompt_constant_names:
                    continue
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    if len(node.value.value) > 80:
                        offenders.append(f"{path.relative_to(ROOT)}:{target.id}")

    assert offenders == []


def test_clarify_stylist_prompt_includes_required_blocks() -> None:
    from compass_backend.instructions.loader import load_model_instructions

    text = load_model_instructions("clarify_stylist.md")
    assert "<role>" in text
    assert "<contract>" in text
    assert "<voice>" in text
    assert "<hard_rules>" in text
    assert text.index("<role>") < text.index("<contract>")
    assert text.index("<contract>") < text.index("<voice>")
    assert text.index("<voice>") < text.index("<hard_rules>")

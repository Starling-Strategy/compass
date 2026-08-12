"""Prompt asset loading for Compass answer-layer style guides."""

from __future__ import annotations

import pytest


def test_answer_layer_prompt_loads_from_answer_style_guides() -> None:
    from compass_backend.instructions.loader import load_answer_style_guide

    text = load_answer_style_guide("default.md")

    assert "You improve the user's final reading experience" in text
    assert "AnswerDraft" in text
    assert "sealed answer brief" in text
    assert "Plain-Spoken Explainer" in text
    assert "allowed_nctq_context" in text
    assert "URL must appear in your" in text
    assert "markdown link" in text
    assert "<role>" in text
    assert "<voice>" in text
    assert "<hard_rules>" in text
    assert text == text.strip()


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "../default.md",
        "answer_style_guides/default.md",
        "default",
        "default.txt",
        "default.md.bak",
    ],
)
def test_answer_style_guide_loader_rejects_traversal_and_non_markdown(
    filename: str,
) -> None:
    from compass_backend.instructions.loader import load_answer_style_guide

    with pytest.raises(ValueError, match="Prompt asset filenames"):
        load_answer_style_guide(filename)


def test_default_answer_style_guide_includes_adjacency_mention_block() -> None:
    from pathlib import Path

    resource_root = Path(__file__).parent.parent / "instructions"
    text = (resource_root / "answer_style_guides" / "default.md").read_text(
        encoding="utf-8"
    )
    assert "<adjacency_mention>" in text
    assert "adjacent_metrics" in text
    assert "no numbers" in text.lower() or "do not include numbers" in text.lower()
    # Prompt uses XML scaffold (HOUSE_STYLE.md); adjacency block sits between
    # <shape> and <context_policy>.
    assert text.index("<adjacency_mention>") > text.index("<shape>")
    assert text.index("<adjacency_mention>") < text.index("<context_policy>")

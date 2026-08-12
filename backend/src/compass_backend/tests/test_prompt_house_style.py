"""House-style lints for prompt assets.

These check the cheapest-to-regress rules in
`src/compass_backend/instructions/HOUSE_STYLE.md`. They are intentionally narrow:
they don't try to validate voice or comprehension, only the mechanical
contract.
"""

from __future__ import annotations

import re
from importlib.resources import files

BANNED_TOKENS = ("NEVER", "ALWAYS", "CRITICAL", "MUST NOT", "DO NOT")
STRUCTURAL_PROMPTS = (
    "planner.md",
    "catalog_adjudicator.md",
    "catalog_toolset.md",
    "criterion_classifier.md",
    "judge.md",
)


def _read(package: str, filename: str) -> str:
    return files(package).joinpath(filename).read_text(encoding="utf-8")


def _strip_hard_rules(text: str) -> str:
    """Remove <hard_rules>...</hard_rules> blocks so emphatic tokens inside
    them don't fail the lint. Emphatic tokens are allowed exactly there.
    """
    return re.sub(r"<hard_rules>.*?</hard_rules>", "", text, flags=re.DOTALL)


def test_structural_prompts_have_no_banned_emphatic_tokens_outside_hard_rules() -> None:
    failures = []
    for filename in STRUCTURAL_PROMPTS:
        text = _read("compass_backend.instructions.model_instructions", filename)
        scrubbed = _strip_hard_rules(text)
        for token in BANNED_TOKENS:
            for match in re.finditer(rf"\b{re.escape(token)}\b", scrubbed):
                line = scrubbed[: match.start()].count("\n") + 1
                failures.append(f"{filename}:{line} — banned token {token!r}")
    assert not failures, "\n".join(failures)


def _ngrams(text: str, n: int = 10) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def test_planner_guidance_snippets_share_no_10gram_with_base_planner() -> None:
    base = _read("compass_backend.instructions.model_instructions", "planner.md")
    base_ngrams = _ngrams(base, n=10)
    failures = []
    snippets_dir = files("compass_backend.instructions.planner_guidance")
    for entry in snippets_dir.iterdir():
        name = entry.name
        if not name.endswith(".md"):
            continue
        snippet = entry.read_text(encoding="utf-8")
        overlap = _ngrams(snippet, n=10) & base_ngrams
        if overlap:
            sample = " ".join(next(iter(overlap)))
            failures.append(f"{name} — shares 10-gram with planner.md: {sample!r}")
    assert not failures, "\n".join(failures)

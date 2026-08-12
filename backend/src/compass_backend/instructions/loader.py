"""Packaged prompt asset loaders for Compass."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import PurePosixPath
from typing import Literal

_MODEL_INSTRUCTION_PACKAGE = "compass_backend.instructions.model_instructions"
_ANSWER_STYLE_GUIDE_PACKAGE = "compass_backend.instructions.answer_style_guides"
_PLANNER_GUIDANCE_PACKAGE = "compass_backend.instructions.planner_guidance"

PromptSection = Literal[
    "model_instructions",
    "planner_guidance",
    "answer_style_guides",
]


def load_model_instructions(filename: str) -> str:
    """Load static Pydantic AI instructions from packaged Markdown."""

    return _load_text_asset(_MODEL_INSTRUCTION_PACKAGE, filename)


def load_answer_style_guide(filename: str) -> str:
    """Load reviewable user-facing answer guidance from packaged Markdown."""

    return _load_text_asset(_ANSWER_STYLE_GUIDE_PACKAGE, filename)


def load_planner_guidance(filename: str) -> str:
    """Load a deterministic planner instruction snippet from packaged Markdown."""

    return _load_text_asset(_PLANNER_GUIDANCE_PACKAGE, filename)


def prompt_text(section: PromptSection, filename: str) -> str:
    """Load one prompt asset by high-level section."""

    if section == "model_instructions":
        return load_model_instructions(filename)
    if section == "answer_style_guides":
        return load_answer_style_guide(filename)
    if section == "planner_guidance":
        return load_planner_guidance(filename)
    raise ValueError(f"Unknown prompt section {section!r}.")


@lru_cache(maxsize=None)
def _load_text_asset(package: str, filename: str) -> str:
    """Load one packaged Markdown asset after validating its filename."""

    _validate_asset_filename(filename)
    return (
        resources.files(package)
        .joinpath(filename)
        .read_text(encoding="utf-8")
        .strip()
    )


def _validate_asset_filename(filename: str) -> None:
    """Reject path traversal and non-Markdown prompt asset names."""

    path = PurePosixPath(filename)
    if (
        not filename
        or path.name != filename
        or path.suffix != ".md"
        or path.stem == ""
    ):
        raise ValueError(
            "Prompt asset filenames must be single Markdown basenames, "
            f"got {filename!r}."
        )

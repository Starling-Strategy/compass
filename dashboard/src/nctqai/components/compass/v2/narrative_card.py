"""Narrative + section prose cards for the Control Tower."""

from __future__ import annotations

from fasthtml.common import Div, H3, P


def NarrativeCard(title: str, body: str, *, kind: str = "default"):
    """A titled prose block. kind ∈ {default, callout, warn}."""
    return Div(
        H3(title, cls="v2-narrative-title"),
        *[P(para, cls="v2-narrative-body") for para in body.strip().split("\n\n")],
        cls=f"v2-narrative v2-narrative-{kind}",
    )

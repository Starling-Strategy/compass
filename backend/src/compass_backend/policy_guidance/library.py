"""Process-lifetime singleton holder for the PolicyGuidanceLibrary.

The library loads once during FastAPI startup and stays immutable for the
process lifetime. Holding it in a module-level singleton (rather than threading
it through every dependency) keeps callers simple — the planner factory, the
renderer, and any future Pydantic validators that need topic enums can call
``get_library()`` without dependency injection plumbing.

Loading is *strict*: ``load_default_library()`` propagates ``LibraryLoadError``
so the FastAPI lifespan refuses to boot on broken content rather than serving
a backend with hallucination-prone fallbacks.
"""

from __future__ import annotations

from pathlib import Path

from compass_backend.policy_guidance.loader import (
    DEFAULT_CONTENT_DIR,
    load_library,
)
from compass_backend.policy_guidance.models import PolicyGuidanceLibrary


class LibraryNotLoadedError(RuntimeError):
    """Raised when ``get_library()`` is called before the lifespan loaded it."""


_LIBRARY: PolicyGuidanceLibrary | None = None


def set_library(library: PolicyGuidanceLibrary) -> None:
    """Replace the process-level singleton. Tests + lifespan call this."""

    global _LIBRARY
    _LIBRARY = library


def get_library() -> PolicyGuidanceLibrary:
    """Return the loaded singleton, raising if the lifespan hasn't run yet."""

    if _LIBRARY is None:
        raise LibraryNotLoadedError(
            "PolicyGuidanceLibrary not loaded. "
            "Call set_library() (tests) or load_default_library() "
            "(FastAPI startup) before reading."
        )
    return _LIBRARY


def reset_library() -> None:
    """Clear the singleton — for tests + restart-on-config-change scenarios."""

    global _LIBRARY
    _LIBRARY = None


def load_default_library(content_dir: Path = DEFAULT_CONTENT_DIR) -> None:
    """Lifespan startup hook: load the markdown library and install it.

    Strict by design: any LibraryLoadError propagates so FastAPI startup
    fails loudly. Better not to boot than to serve broken citations.
    """

    library = load_library(content_dir)
    set_library(library)

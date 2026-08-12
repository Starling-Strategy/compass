"""Shared helpers for Compass route handlers.

Two patterns the Compass surface kept reinventing:

- Access check + a Forbidden Layout response for non-Compass users.
- "Call a sync service, log + fall back to a default on failure" wrappers
  around every read so one broken aggregate doesn't blank the page.

Plus a tiny ``run_in_thread`` shim so handlers can push synchronous
``run_sql`` calls off the event loop without sprinkling ``asyncio.to_thread``
imports through every route file.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

from starlette.requests import Request

from nctqai.routes._auth import require_section

logger = logging.getLogger(__name__)

T = TypeVar("T")


def require_compass(request: Request):
    """Section gate for Compass — open to ALL roles (#1806).

    The Compass monitoring surface (Overview, Conversations + detail, Flagged
    Issues, Data Universe) is visible to viewer / analyst / power_user / admin
    via ``section_roles["compass"]`` in ``models.auth``. Returns
    ``(user, deny_or_None)``; do ``if deny: return deny``. Thin wrapper over the
    shared :func:`nctqai.routes._auth.require_section` so every section gates the
    same way. For HTMX partials use :func:`require_compass_partial`.
    """
    return require_section(request, "compass")


def require_compass_admin(request: Request):
    """Admin-only Compass gate — Scenarios, Scorecard, Operations, traces.

    All roles keep the monitoring surface (Overview, Conversations, Data
    Universe, Flagged Issues) via :func:`require_compass`; only the eval-builder
    and operations tabs require admin. For HTMX partials use
    :func:`require_compass_admin_partial`.
    """
    return require_section(request, "compass", admin=True)


def require_compass_partial(request: Request):
    """HTMX-partial Compass gate — denies with ``Div("Forbidden")``, not a page."""
    return require_section(request, "compass", partial=True)


def require_compass_admin_partial(request: Request):
    """HTMX-partial admin-only Compass gate — denies with ``Div("Forbidden")``."""
    return require_section(request, "compass", admin=True, partial=True)


def safe(fn: Callable[..., T], default: T, *, name: str = "") -> T:
    """Call ``fn()``, log any exception, return ``default`` on failure.

    Mirrors the wrapper that data_universe.py grew on its own; lifted here
    so every Compass page can use the same shape.
    """
    try:
        return fn()
    except Exception:
        logger.exception("compass service call failed: %s", name or getattr(fn, "__name__", "?"))
        return default


async def run_in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``await asyncio.to_thread`` with a stable name for grep-ability."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def gather_safe(*coros: Awaitable[Any]) -> list[Any]:
    """Run coroutines in parallel; on any single failure, the awaiter sees
    the exception but the others still complete. Callers should already have
    wrapped each branch in ``safe`` for graceful degradation.
    """
    return await asyncio.gather(*coros, return_exceptions=False)

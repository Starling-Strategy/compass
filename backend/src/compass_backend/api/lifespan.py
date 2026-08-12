"""Application lifecycle hooks for the transport layer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

Hook = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True)
class LifespanHooks:
    """Startup and shutdown callbacks owned by admitted layers."""

    startup: tuple[Hook, ...] = ()
    shutdown: tuple[Hook, ...] = ()


async def _run_hook(hook: Hook) -> None:
    result = hook()
    if hasattr(result, "__await__"):
        await result


def create_lifespan(hooks: LifespanHooks | None = None):
    """Create a FastAPI lifespan handler from optional hooks."""

    active_hooks = hooks or LifespanHooks()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for hook in active_hooks.startup:
            await _run_hook(hook)
        try:
            yield
        finally:
            for hook in reversed(active_hooks.shutdown):
                await _run_hook(hook)

    return lifespan

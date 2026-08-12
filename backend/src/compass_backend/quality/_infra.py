"""Shared infra-availability checks for the quality evaluators."""

from __future__ import annotations

import os

_GATEWAY_API_KEY_ENV = "PYDANTIC_AI_GATEWAY_API_KEY"


def gateway_api_key_present() -> bool:
    """Return whether the Pydantic AI Gateway key is set in the environment.

    Read from ``os.environ`` (not cached ``Settings``) on purpose: the judge and
    scenario-fit evaluators surface an ``outcome='error'`` skip verdict when the
    key is absent, and the test suite toggles this var at runtime to exercise
    that path. A single helper keeps the env-var name in one place so the two
    evaluators can't drift.
    """

    return bool(os.environ.get(_GATEWAY_API_KEY_ENV))

"""Production boot-guard: refuse to boot with auth disabled outside dev (#22).

AuthMiddleware has a dev escape hatch — when ``NCTQAI_AUTH_DISABLED`` is set it
injects an admin ``_DEV_USER`` on every route, bypassing session auth entirely.
The backend already guards the mirror hatch (``compass_backend/api/app.py``'s
``_assert_auth_required_outside_dev``): it refuses to boot staging/prod with auth
off. This test covers the dashboard's matching guard in ``nctqai/main.py``.

The guard takes a ``Config`` argument, so tests pass constructed instances —
init kwargs outrank env/.env in pydantic-settings, so no env mutation or module
reload is needed.
"""

from __future__ import annotations

import pytest

from nctqai.config import Config
from nctqai.main import _assert_auth_required_outside_dev


def test_boot_guard_raises_in_production_when_auth_disabled() -> None:
    with pytest.raises(RuntimeError, match="Refusing to boot"):
        _assert_auth_required_outside_dev(
            Config(environment="production", auth_disabled=True)
        )


def test_boot_guard_raises_in_staging_when_auth_disabled() -> None:
    with pytest.raises(RuntimeError, match="Refusing to boot"):
        _assert_auth_required_outside_dev(
            Config(environment="staging", auth_disabled=True)
        )


def test_boot_guard_noop_in_development_with_auth_disabled() -> None:
    """Development is the one sanctioned place for the auth-off escape hatch —
    the guard must NOT raise there."""
    _assert_auth_required_outside_dev(
        Config(environment="development", auth_disabled=True)
    )


def test_boot_guard_noop_outside_dev_when_auth_enabled() -> None:
    """A normal staging/prod boot (auth on) must not be blocked."""
    _assert_auth_required_outside_dev(
        Config(environment="production", auth_disabled=False)
    )

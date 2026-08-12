"""NCTQ.ai Dashboard — FastHTML + MonsterUI Application.

Unified dashboard for the NCTQ AI system: Metric Calculator, Documents,
and Compass sections.

Run with:
    PYTHONPATH=src uvicorn nctqai.main:app --port 5001 --reload --reload-dir src/nctqai
"""

import logging
import os

from fasthtml.common import Script, fast_app, serve
from monsterui.all import Theme
from starlette.responses import RedirectResponse

# Configure structured logging (JSON lines visible in `docker logs piper-staging`)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

from nctqai.errors import not_found, server_error
from nctqai.middleware import AuthMiddleware, CacheMiddleware, CSRFMiddleware
from nctqai.config import Config
from nctqai.routes.admin import register_admin_routes
from nctqai.routes.auth import register_auth_routes
from nctqai.routes.compass import register_compass_routes
from nctqai.routes.docs import register_docs_routes
from nctqai.routes.home import register_home_routes
from nctqai.routes.journal import register_journal_routes
from nctqai.routes.mc import register_mc_routes
from nctqai.theme import CUSTOM_CSS

# ---------------------------------------------------------------------------
# Production boot-guard (audit #22)
# ---------------------------------------------------------------------------
def _assert_auth_required_outside_dev(config: Config) -> None:
    """Refuse to boot staging/prod with auth disabled.

    Mirrors the backend guard in compass_backend/api/app.py: the dev-only
    escape hatch (NCTQAI_AUTH_DISABLED, which injects an admin _DEV_USER on
    every route in AuthMiddleware) must never be active outside development.
    """
    if config.environment != "development" and config.auth_disabled:
        raise RuntimeError(
            "Refusing to boot: auth_disabled=True is only permitted when "
            f"environment=='development' (got environment={config.environment!r})."
        )


# Fail fast at import — before the app is built — if a staging/prod deploy
# left auth off. Import of this module is the boot; no app should be served.
_assert_auth_required_outside_dev(Config())

# ---------------------------------------------------------------------------
# MonsterUI Theme
# ---------------------------------------------------------------------------
theme = Theme.blue
# pico=False: MonsterUI handles all styling, PicoCSS is redundant
# force_light: MonsterUI's mode="light" only removes .dark class but doesn't add .light,
#   leaving Franken-UI in "auto" mode which follows OS preference. Adding .light forces it.
force_light = Script('document.documentElement.classList.add("light");')
# exception_handlers: FastHTML wraps these so the returned components render
# with the themed headers above and the dict-key status code is applied — a
# bad URL gets a styled 404 inside the dashboard chrome, not bare plain text.
app, rt = fast_app(
    pico=False,
    hdrs=[*theme.headers(mode="light"), force_light, CUSTOM_CSS],
    exception_handlers={404: not_found, 500: server_error},
)

# ---------------------------------------------------------------------------
# Logfire (optional) — must run BEFORE add_middleware so Starlette
# instrumentation hooks wrap the middleware stack rather than missing it.
# ---------------------------------------------------------------------------
try:
    import logfire

    _logfire_token = os.environ.get("LOGFIRE_TOKEN") or None
    logfire.configure(
        token=_logfire_token,
        service_name="nctqai-dashboard",
        environment=os.environ.get("ENV", "development"),
        send_to_logfire="if-token-present",
    )
    logfire.instrument_starlette(app)
except Exception:
    # Observability is optional: a missing/invalid token or a logfire import
    # problem must never block the dashboard from starting.
    pass

# Middleware runs in reverse-registration order — CacheMiddleware is outermost
# (just adds headers, no auth state access), then CSRFMiddleware rejects
# cross-site POSTs, then AuthMiddleware does session lookup.
app.add_middleware(AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(CacheMiddleware)

# ---------------------------------------------------------------------------
# Section routes
# ---------------------------------------------------------------------------
register_admin_routes(rt)
register_auth_routes(rt)
register_home_routes(rt)
register_docs_routes(rt)
register_journal_routes(rt)
register_mc_routes(rt)
register_compass_routes(rt)

# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------


@rt("/health")
def get():
    """Health check endpoint."""
    return {"status": "ok"}


@rt("/mc")
def mc_redirect():
    """Redirect /mc to the districts list."""
    return RedirectResponse("/mc/districts", status_code=302)


@rt("/compass")
def compass_redirect():
    """Redirect /compass to the Compass overview."""
    return RedirectResponse("/compass/overview", status_code=302)


# ---------------------------------------------------------------------------
# Clean up expired auth rows on startup
# ---------------------------------------------------------------------------
try:
    from nctqai.services.auth import cleanup_expired
    result = cleanup_expired()
    logger.info("Auth cleanup on startup: %s", result)
except Exception:
    logger.exception("Auth cleanup failed (non-fatal)")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    serve()

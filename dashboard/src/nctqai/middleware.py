"""Auth + CSRF + cache middleware for the NCTQ.ai dashboard.

AuthMiddleware gates access behind session-cookie auth.
CSRFMiddleware blocks cross-site POSTs unless HTMX-originated or from an
allowed origin.
CacheMiddleware adds Cache-Control headers to Compass GET routes so the
browser can serve repeated navigations from its local cache instead of
hitting the server again — the server-side latency is now fast (~150 ms)
but an HTTP round-trip is still slower than a browser cache hit (~10 ms).
"""

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse

from nctqai.config import Config
from nctqai.models.auth import User
from nctqai.services.auth import get_session_user
from nctqai.routes.compass._helpers import run_in_thread

# Routes that don't require authentication
PUBLIC_ROUTES = frozenset({
    "/login",
    "/auth/send-code",
    "/auth/verify",
    "/health",
})

# Routes exempted from CSRF enforcement. /auth/send-code and /auth/verify are
# the login flow itself — there's no session to protect — and they already
# rate-limit per-IP inside send_otp/verify_otp.
CSRF_EXEMPT_ROUTES = frozenset({
    "/auth/send-code",
    "/auth/verify",
    "/health",
})

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Fake admin user for dev mode (auth disabled)
_DEV_USER = User(id=0, email="dev@localhost", name="Dev User", role="admin", is_active=True)

# Singleton — config is immutable at runtime, no need to re-parse .env per request
_config = Config()


def _allowed_hosts() -> frozenset[str]:
    """Parse NCTQAI_ALLOWED_ORIGINS into a set of host[:port] strings."""
    raw = _config.allowed_origins or ""
    hosts: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parsed = urlparse(entry)
        host = parsed.netloc or parsed.path
        if host:
            hosts.add(host)
    return frozenset(hosts)


_ALLOWED_HOSTS = _allowed_hosts()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path

        # Auth disabled — inject dev admin user on all routes
        if _config.auth_disabled:
            request.state.user = _DEV_USER
            return await call_next(request)

        # Public routes — no auth required
        if path in PUBLIC_ROUTES or path.startswith("/static"):
            request.state.user = None
            return await call_next(request)

        # Check session cookie
        session_id = request.cookies.get("nctqai_session")
        user = await run_in_thread(get_session_user, session_id) if session_id else None

        if not user:
            return RedirectResponse("/login", status_code=302)

        request.state.user = user
        response = await call_next(request)
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Block cross-site POST/PUT/PATCH/DELETE unless HTMX or an allowed origin.

    A request passes iff at least one is true:
      * The method is safe (GET, HEAD, OPTIONS).
      * The path is on the CSRF exempt list.
      * The request carries `HX-Request: true` (HTMX sets this on every
        `hx-*` action; a cross-origin form post cannot).
      * The request's `Origin` or `Referer` host matches
        `NCTQAI_ALLOWED_ORIGINS`.
    """

    async def dispatch(self, request, call_next):
        method = request.method.upper()
        if method not in _UNSAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path in CSRF_EXEMPT_ROUTES or path.startswith("/static"):
            return await call_next(request)

        if request.headers.get("hx-request", "").lower() == "true":
            return await call_next(request)

        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            parsed = urlparse(source)
            if parsed.netloc and parsed.netloc in _ALLOWED_HOSTS:
                return await call_next(request)

        return PlainTextResponse(
            "Forbidden: cross-site request blocked.",
            status_code=403,
        )


# ── HTTP Cache ──────────────────────────────────────────────────────

# Compass dashboard pages serve aggregated stats that change slowly
# (new sessions arrive over minutes, not seconds). A short browser cache
# eliminates the HTTP round-trip on repeated navigations — the biggest
# remaining latency gap after the server-side snapshot-blob fix.
#
# Key design choices:
#   * ``private`` — the dashboard is behind session-cookie auth; without
#     ``private``, a shared cache (CDN, corporate proxy) could serve one
#     user's page to another.
#   * ``max-age`` — how long the browser serves from cache before asking
#     the server again.
#   * ``stale-while-revalidate`` — after max-age expires, the browser
#     shows stale content while fetching fresh in the background. The user
#     never waits; the page just silently refreshes a moment later.
#
# Interactive HTMX partials get shorter max-age than full pages because
# the user expects immediate feedback on a click or filter change.


# (prefix, max-age seconds) — longest prefix match wins.
# Ordered from most-specific (longest) to least-specific so the loop
# can break on the first match.
_COMPASS_CACHE_RULES: list[tuple[str, int]] = [
    # HTMX partials — interactive, user expects responsiveness
    ("/compass/conversations/detail", 10),
    ("/compass/conversations/list", 15),
    # Full Compass pages — aggregate stats, changes slowly
    ("/compass/conversations", 30),
    ("/compass/quality", 60),
    ("/compass/overview", 60),
    ("/compass/data-universe", 60),
    ("/compass/operations", 60),
    ("/compass/scenarios", 60),
]

_COMPASS_STALE_WHILE_REVALIDATE = 300  # 5 minutes


def _cache_max_age(path: str) -> int | None:
    """Return the max-age for a Compass GET path, or None if uncached."""
    for prefix, max_age in _COMPASS_CACHE_RULES:
        if path.startswith(prefix):
            return max_age
    return None


class CacheMiddleware(BaseHTTPMiddleware):
    """Add Cache-Control headers to Compass GET responses.

    Only Compass GET routes get cache headers. POST (flags, reports) and
    non-Compass pages (Metric Calculator writes data, auth flows) are
    never cached. Health checks are also excluded.

    Only *successful* (200) responses are cached. This middleware runs
    outermost, so it sees AuthMiddleware's ``302 -> /login`` redirect for an
    unauthenticated request; caching that 302 would make the browser replay
    the login bounce after the user signs in (a login loop). The same guard
    keeps transient 404 panes and 4xx errors out of the cache.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        if request.method != "GET":
            return response

        # Only cache successful page bodies — never a redirect (302 -> /login)
        # or an error. See the class docstring for the login-loop this avoids.
        if response.status_code != 200:
            return response

        path = request.url.path

        # Never cache health checks or auth routes
        if path == "/health" or path.startswith("/auth"):
            return response

        max_age = _cache_max_age(path)
        if max_age is not None:
            response.headers[
                "Cache-Control"
            ] = f"private, max-age={max_age}, stale-while-revalidate={_COMPASS_STALE_WHILE_REVALIDATE}"

        return response

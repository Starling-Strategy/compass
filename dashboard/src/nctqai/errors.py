"""Styled 404 / 500 error pages for the dashboard.

Rendered inside the standard dashboard chrome (TopNav + Container) with a link
back to the Overview, instead of Starlette's bare plain-text error response
(style-audit finding G3). These are passed to ``fast_app(exception_handlers=...)``
in ``main`` so FastHTML renders the returned components with the themed headers
*and* applies the HTTP status code (see ``fasthtml.core._wrap_ex``).
"""

from __future__ import annotations

from fasthtml.common import A, Div, P

from nctqai.layout import Layout


def _error_page(req, *, title: str, message: str):
    """Build a styled error page inside the dashboard chrome.

    Reuses the authenticated user from request state when present so the top
    nav renders normally; falls back to the public chrome (``user=None``) when
    the error fires before auth has run or on a request without state.
    """
    user = getattr(getattr(req, "state", None), "user", None)
    content = Div(
        P(message, cls="text-muted"),
        P(A("← Back to the dashboard", href="/compass/overview")),
        cls="empty-state-lg",
    )
    # Layout renders the title as the page H1; the empty subtitle is omitted.
    return Layout(title, "", content, user=user)


def not_found(req, exc):
    return _error_page(
        req,
        title="404 — Page not found",
        message="That page doesn’t exist. It may have moved, or the link was mistyped.",
    )


def server_error(req, exc):
    return _error_page(
        req,
        title="500 — Something went wrong",
        message=(
            "An unexpected error occurred on our side. Please try again, "
            "or head back to the dashboard."
        ),
    )

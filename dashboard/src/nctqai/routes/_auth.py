"""Shared route authorization guards for the whole dashboard.

One pattern for every section instead of three hand-rolled styles.
``require_section(request, section)`` returns ``(user, deny_or_None)``; the caller
does ``if deny: return deny``. ``deny`` is a full-page Forbidden Layout, or a bare
``Div("Forbidden")`` when ``partial=True`` (HTMX endpoints that swap into a panel).
``admin=True`` additionally requires ``user.is_admin()``.

Section access is the single central map — ``models.auth.User.can_access`` /
``is_admin`` (the ``Role`` enum). Don't re-implement role checks inside route
handlers; call this so every page and partial gates the same way.
"""

from __future__ import annotations

from fasthtml.common import Div, P
from starlette.requests import Request

from nctqai.layout import Layout


def require_section(
    request: Request,
    section: str,
    *,
    admin: bool = False,
    partial: bool = False,
):
    """Gate a route on section access (and optionally admin).

    Returns ``(user, deny)`` where ``deny`` is ``None`` when allowed, a
    ``Div("Forbidden")`` when ``partial`` and denied, else a full-page Forbidden
    ``Layout``.
    """
    user = getattr(request.state, "user", None)
    allowed = bool(
        user and user.can_access(section) and (user.is_admin() if admin else True)
    )
    if allowed:
        return user, None
    if partial:
        return user, Div("Forbidden")
    return user, Layout(
        "Forbidden",
        "",
        P("You don't have access to this section."),
        user=user,
    )

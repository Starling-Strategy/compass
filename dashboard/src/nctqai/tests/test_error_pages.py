"""Styled 404/500 error pages render inside the dashboard chrome (G3).

Exercises the handler functions directly (no full-app import, which would
trigger the startup DB cleanup) — they return the Layout shell, so a bad URL
gets branded chrome + a way back instead of Starlette's bare plain text.
"""

from __future__ import annotations

from types import SimpleNamespace

from fasthtml.common import to_xml

from nctqai.errors import not_found, server_error


def _req(user=None):
    return SimpleNamespace(state=SimpleNamespace(user=user))


def _html(res) -> str:
    # Layout returns (Title, Main); render both halves to one HTML string.
    return "".join(to_xml(part) for part in res)


def test_not_found_renders_styled_page_with_back_link():
    html = _html(not_found(_req(), None))
    assert "404" in html
    assert "Page not found" in html
    assert 'href="/compass/overview"' in html
    assert "Back to the dashboard" in html
    # A full themed page, not a bare fragment.
    assert "<title>" in html.lower()


def test_server_error_renders_styled_page_with_back_link():
    html = _html(server_error(_req(), None))
    assert "500" in html
    assert 'href="/compass/overview"' in html


def test_error_page_handles_missing_request_state():
    # An exception can fire before AuthMiddleware sets request.state.user.
    html = _html(not_found(SimpleNamespace(), None))
    assert "404" in html

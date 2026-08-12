"""Route tests for the Journal section (fast_app + TestClient)."""
from __future__ import annotations

from fasthtml.common import fast_app
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

import nctqai.routes.journal as journal
from nctqai.models.auth import User
from nctqai.models.journal import Lesson

_ADMIN = User(id=0, email="dev@localhost", name="Dev", role="admin")
_POWER_USER = User(id=9, email="p@localhost", name="Power", role="power_user")


def _lesson(slug="1-x", title="Title", body="## Heading\n\nText.", **kw) -> Lesson:
    return Lesson(slug=slug, title=title, hook="hook", body_markdown=body, **kw)


def _client(monkeypatch, lessons: list[Lesson], user: User = _ADMIN) -> TestClient:
    monkeypatch.setattr(journal, "list_lessons", lambda: lessons)
    monkeypatch.setattr(
        journal,
        "get_lesson",
        lambda slug: next((l for l in lessons if l.slug == slug), None),
    )

    app, rt = fast_app(pico=False)

    class _InjectUser(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = user
            return await call_next(request)

    app.add_middleware(_InjectUser)
    journal.register_journal_routes(rt)
    return TestClient(app)


def test_feed_lists_a_card_per_lesson(monkeypatch):
    client = _client(monkeypatch, [_lesson(slug="a", title="Alpha"), _lesson(slug="b", title="Beta")])
    resp = client.get("/journal")
    assert resp.status_code == 200
    assert "Alpha" in resp.text and "Beta" in resp.text
    assert resp.text.count("overview-panel") == 2
    assert 'href="/journal/a"' in resp.text


def test_feed_empty_state(monkeypatch):
    client = _client(monkeypatch, [])
    resp = client.get("/journal")
    assert resp.status_code == 200
    assert "No lessons yet" in resp.text


def test_lesson_page_renders_markdown(monkeypatch):
    client = _client(monkeypatch, [_lesson(slug="a", title="Alpha", body="## Big\n\nHello.")])
    resp = client.get("/journal/a")
    assert resp.status_code == 200
    assert "<h2>Big</h2>" in resp.text
    assert "Hello." in resp.text
    assert "Back to Journal" in resp.text


def test_lesson_page_not_found(monkeypatch):
    client = _client(monkeypatch, [])
    resp = client.get("/journal/missing")
    assert "Lesson not found" in resp.text


def test_feed_denies_non_admin(monkeypatch):
    client = _client(monkeypatch, [_lesson(slug="a", title="Alpha")], user=_POWER_USER)
    resp = client.get("/journal")
    assert "Alpha" not in resp.text
    assert "don't have access" in resp.text


def test_lesson_page_denies_non_admin(monkeypatch):
    client = _client(monkeypatch, [_lesson(slug="a", title="Alpha", body="## Big\n\nHi.")], user=_POWER_USER)
    resp = client.get("/journal/a")
    assert "<h2>Big</h2>" not in resp.text
    assert "don't have access" in resp.text

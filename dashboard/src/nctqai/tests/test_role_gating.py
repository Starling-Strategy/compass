"""Role single-source + per-tab Compass nav gating + section guards.

Policy under test (Compass, #1806): the monitoring surface — Overview,
Conversations, Data Universe, AND Flagged Issues (/compass/quality/reports) — is
visible to ALL roles; only the eval-builder tabs Scenarios / Scorecard stay
admin-only. Section access runs through the central
``models.auth.User.can_access`` map, enforced by the shared
``routes._auth.require_section`` guard (and the Compass wrappers).
"""
from __future__ import annotations

from types import SimpleNamespace

import fasthtml.common as fh

from nctqai.components.nav import _TAB_ROLES, SubNav
from nctqai.models.auth import Role, User
from nctqai.routes._auth import require_section
from nctqai.routes.admin import VALID_ROLES
from nctqai.routes.compass._helpers import require_compass_admin


def _user(role: str) -> User:
    return User(id=1, email=f"{role}@example.com", name=role.title(), role=role)


def _request(user) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=user))


# Compass tab policy ---------------------------------------------------------
# Monitoring tabs visible to ALL roles (#1806) — Flagged Issues (the
# /compass/quality/reports route) joined this set when the queue was opened up.
_ALL_ROLES_TABS = (
    "/compass/overview",
    "/compass/conversations",
    "/compass/data-universe",
    "/compass/quality/reports",
)
# Eval-builder tabs that stay admin-only.
_ADMIN_ONLY_TABS = (
    "/compass/scenarios",
    "/compass/quality/scorecard",
)


# --- Role single source -----------------------------------------------------
def test_role_enum_matches_db_constraint():
    """Role mirrors the CHECK constraint in migrations/001_auth_tables.sql.

    That migration is append-only and already applied, so this static set is
    the contract; if someone adds a role to the enum they must also ship a
    new migration that widens the DB CHECK.
    """
    assert {r.value for r in Role} == {"viewer", "analyst", "power_user", "admin"}


def test_valid_roles_derives_from_enum():
    assert VALID_ROLES == [r.value for r in Role]


# --- Per-tab Compass nav gating --------------------------------------------
def test_tab_roles_covers_exactly_the_admin_only_tabs():
    """The nav's admin-only tab map matches the policy, all gated to admin.

    #1806: Flagged Issues (/compass/quality/reports) left this map — only the
    eval-builder tabs Scenarios + Scorecard remain admin-gated here.
    """
    assert set(_TAB_ROLES) == set(_ADMIN_ONLY_TABS)
    assert all(roles == (Role.ADMIN,) for roles in _TAB_ROLES.values())


def test_subnav_admin_sees_all_compass_tabs():
    html = fh.to_xml(SubNav("compass", user=_user("admin")))
    for href in _ALL_ROLES_TABS + _ADMIN_ONLY_TABS:
        assert f'href="{href}"' in html, href


def test_subnav_power_user_sees_monitoring_tabs_not_eval_builder():
    html = fh.to_xml(SubNav("compass", user=_user("power_user")))
    for href in _ALL_ROLES_TABS:
        assert f'href="{href}"' in html, href
    for href in _ADMIN_ONLY_TABS:
        assert f'href="{href}"' not in html, href


def test_subnav_viewer_and_analyst_see_flagged_issues_not_eval_builder():
    """#1806: viewer + analyst now see the monitoring surface incl. Flagged
    Issues, but still not the admin-only Scenarios / Scorecard tabs."""
    for role in ("viewer", "analyst"):
        html = fh.to_xml(SubNav("compass", user=_user(role)))
        assert 'href="/compass/quality/reports"' in html, role
        for href in _ADMIN_ONLY_TABS:
            assert f'href="{href}"' not in html, (role, href)


def test_subnav_no_user_hides_admin_only_tabs():
    """Defensive: an un-passed user hides gated tabs rather than leaking them."""
    html = fh.to_xml(SubNav("compass"))
    for href in _ADMIN_ONLY_TABS:
        assert f'href="{href}"' not in html, href
    assert 'href="/compass/overview"' in html


# --- Shared section guard (routes/_auth.require_section) --------------------
def test_require_section_allows_member():
    _, deny = require_section(_request(_user("analyst")), "docs")
    assert deny is None


def test_require_section_denies_non_member():
    # viewer is not in section_roles["pa"] (power_user + admin only).
    _, deny = require_section(_request(_user("viewer")), "pa")
    assert deny is not None


def test_require_section_compass_open_to_all_roles():
    """#1806: every role can reach the Compass monitoring section (the gate the
    Flagged Issues page + conversation backlinks share)."""
    for role in ("viewer", "analyst", "power_user", "admin"):
        _, deny = require_section(_request(_user(role)), "compass")
        assert deny is None, role


def test_require_section_admin_flag_denies_power_user():
    _, deny = require_section(_request(_user("power_user")), "compass", admin=True)
    assert deny is not None


def test_require_section_admin_flag_allows_admin():
    _, deny = require_section(_request(_user("admin")), "compass", admin=True)
    assert deny is None


def test_require_section_partial_denies_with_bare_div():
    # viewer is denied the "pa" section; the partial deny is a bare Div.
    _, deny = require_section(_request(_user("viewer")), "pa", partial=True)
    assert deny is not None
    assert "<div>Forbidden</div>" in fh.to_xml(deny)  # not a full page


def test_require_section_denies_anonymous():
    _, deny = require_section(_request(None), "compass")
    assert deny is not None


# --- Compass admin wrapper (Reports/Scenarios/Scorecard/Operations/traces) --
def test_require_compass_admin_allows_admin():
    user, deny = require_compass_admin(_request(_user("admin")))
    assert deny is None
    assert user.is_admin()


def test_require_compass_admin_denies_power_user():
    _, deny = require_compass_admin(_request(_user("power_user")))
    assert deny is not None


def test_require_compass_admin_denies_anonymous():
    _, deny = require_compass_admin(_request(None))
    assert deny is not None


# --- Admin section still resolves to admin-only -----------------------------
def test_subnav_reports_visible_to_admin():
    html = fh.to_xml(SubNav("compass", user=_user("admin")))
    assert 'href="/compass/quality/reports"' in html


# --- Route-wrapper gate (regression guard: the @rt handler must deny, not
#     just the helper). Captures the registered handler and calls it with a
#     power_user; the admin gate must short-circuit before any DB/async work.
def _captured_routes(register_fn) -> dict:
    routes: dict = {}

    class _RT:
        def __call__(self, path, **_kw):
            def deco(fn):
                routes[path] = fn
                return fn
            return deco

    register_fn(_RT())
    return routes


def _deny_html(resp) -> str:
    parts = resp if isinstance(resp, tuple) else (resp,)
    return "".join(fh.to_xml(p) for p in parts).lower()


def test_scorecard_route_wrapper_denies_power_user():
    from nctqai.routes.compass.quality.scorecard import register
    handler = _captured_routes(register)["/compass/quality/scorecard"]
    html = _deny_html(handler(_request(_user("power_user"))))
    assert "don't have access" in html or "forbidden" in html


def test_scenarios_route_wrapper_denies_power_user():
    import asyncio

    from nctqai.routes.compass.scenarios import register
    handler = _captured_routes(register)["/compass/scenarios"]
    html = _deny_html(asyncio.run(handler(_request(_user("power_user")))))
    assert "don't have access" in html or "forbidden" in html


def test_operations_route_wrapper_denies_power_user():
    import asyncio

    from nctqai.routes.compass.operations import register
    handler = _captured_routes(register)["/compass/operations"]
    html = _deny_html(asyncio.run(handler(_request(_user("power_user")))))
    assert "don't have access" in html or "forbidden" in html


def test_reports_status_route_denies_anonymous_with_partial():
    """#1446 + #1806: the HTMX status endpoint denies with a bare Div, not a full
    page, so a denied POST doesn't mangle the #quality-reports-table swap target.

    As of #1806 every Compass role (incl. power_user) may change a flag's status,
    so the denial case is now an anonymous (no-user) request, not a power_user.
    """
    import asyncio

    from nctqai.routes.compass.quality.reports import register
    handler = _captured_routes(register)["/compass/quality/reports/{report_id}/status"]
    resp = asyncio.run(handler(_request(None), report_id="x"))
    html = _deny_html(resp)
    assert "<div>forbidden</div>" in html       # partial deny shape
    assert "<title" not in html and "<main" not in html  # not a full-page Layout

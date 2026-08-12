"""Navigation components for the NCTQ.ai dashboard.

TopNav: navy navigation bar with section links and avatar dropdown.
SubNav: section-specific tab navigation with optional annotation.
"""

from fasthtml.common import A, Button, Div, Form, Hr, Input, Label, Li, Nav, NotStr, P, Span, Ul
from monsterui.all import H4, DivLAligned

from nctqai.models.auth import Role

# Just the TQ monogram (no wordmark) for the top-nav brand — cropped viewBox of
# the full NCTQ logo. Fills (logo-primary / logo-secondary) are colored in
# theme.py for the navy bar (white mark + light-blue swoosh).
_LOGO_ICON_SVG = (
    '<svg class="nav-brand-logo" viewBox="0 0 70 82" xmlns="http://www.w3.org/2000/svg"'
    ' role="img" aria-label="NCTQ">'
    '<g class="logo-primary">'
    '<path d="M43.9,74.1c-3.2-1.4-3.6-4.5-3.6-9.6V3.3h-11v61.2c0,5.1-.3,8.1-3.6,9.6-3.9,1.8-8.4.1-8.4,2.2c 0 .8, .6 1, 1.7 1h31.6c1.2,0,1.7-.3,1.7-1,0-2.1-4.5-.4-8.4-2.2Z"></path>'
    '<path d="M68.4,26.5l-5.8-24.2H7.2L1.4,26.5c0,.3-.2,1.1-.2,1.3,0,.4.2.5.4.5.2,0,.4,0,.6,0,1.6,0,1.6-4.5,5.3-9.7,5.5-8,12.8-12.7,21.9-14h11s0,0,0,0h0c9.1,1.3,16.4,6,22,14,3.7,5.2,3.6,9.7,5.3,9.7s.4,0,.5,0c.3,0,.5,0,.5-.4,0,0,0-.1,0-.1,0-.1-.1-1-.2-1.3Z"></path>'
    "</g>"
    '<g class="logo-secondary">'
    '<path d="M68.2,63.8c-7.3,9.2-12.5,7.3-31.5-.3,18.2-.6,32.2-14.7,32.2-31.6S59.4,6.6,46.3,2.3h-5.8v2.2c8.2,3.5,14.2,14.4,14.2,27.4s-8.8,28.6-19.7,28.6-19.7-12.8-19.7-28.6S21.3,7.8,29.6,4.4v-2.1h-6C10.4,6.6.9,18.2.9,31.9s6.8,21.8,17,27.2c-8.8.5-11.5,6.1-12.2,8.3-.1.3.3.6.5.4,11.8-9.1,15.3-4.1,21.7-.3,5.9,3.5,13.2,10.6,24.4,9.8,13.2-1,16.5-13.4,16.5-13.4-.3,0-.5-.3-.7-.1Z"></path>'
    "</g>"
    "</svg>"
)

# ── Section definitions ──────────────────────────────────────
# Each section maps to a list of (label, url) tab definitions.
SECTIONS: dict[str, list[tuple[str, str]]] = {
    "mc": [
        ("Districts", "/mc/districts"),
        ("Questions", "/mc/questions"),
        ("Policies", "/mc/policies"),
        ("Batches", "/mc/batches"),
    ],
    "docs": [
        ("District Documents", "/docs"),
        ("NCTQ Publications", "/docs/publications"),
    ],
    "compass": [
        ("Overview", "/compass/overview"),
        ("Conversations", "/compass/conversations"),
        ("Data Universe", "/compass/data-universe"),
        ("Scenarios", "/compass/scenarios"),
        ("Scorecard", "/compass/quality/scorecard"),
        # Label is "Flagged Issues" but the URL stays /compass/quality/reports —
        # a relabel-only realignment (DASH-R6). Renaming the route would break
        # the hardcoded references (this tab, the route's _REPORTS_URL, and the
        # sub_nav= args) for no MVP gain. As of #1806 this tab is NO LONGER in
        # _TAB_ROLES — Flagged Issues follows the now-all-roles compass section.
        ("Flagged Issues", "/compass/quality/reports"),
    ],
    "journal": [
        ("Lessons", "/journal"),
    ],
}

# Tabs that require a stronger role than their section's baseline.
# url -> roles allowed to see the tab. A url absent here is visible to anyone
# with section access. Mirrors the section_roles shape in models/auth.User.
# Compass policy (#1806): the monitoring surface — Overview, Conversations,
# Data Universe, AND Flagged Issues (/compass/quality/reports) — is visible to
# ALL roles, so those URLs are absent here. Only the eval-BUILDER tabs,
# Scenarios and Scorecard, stay admin-only (matched by the admin route guards in
# each handler — hiding a tab is cosmetic, the route check is the real gate).
_TAB_ROLES: dict[str, tuple[str, ...]] = {
    "/compass/scenarios": (Role.ADMIN,),
    "/compass/quality/scorecard": (Role.ADMIN,),
}

# Top-level nav links: (label, url, section_key)
_TOP_LINKS = [
    ("Home", "/", None),
    ("Metric Calculator", "/mc/districts", "mc"),
    ("Documents", "/docs", "docs"),
    ("Compass", "/compass/overview", "compass"),
    ("Journal", "/journal", "journal"),
    ("Admin", "/admin/users", "admin"),
]


def _user_initials(name: str) -> str:
    """Extract up to 2 uppercase initials from a display name."""
    parts = name.strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


_CARET_SVG = (
    '<svg class="nav-caret" width="11" height="11" viewBox="0 0 12 12" aria-hidden="true">'
    '<path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.5"'
    ' stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def _visible_sub_items(section_key: str | None, user) -> list[tuple[str, str]]:
    """Sub-page (label, url) pairs for a section, filtered by per-tab role gates."""
    if not section_key:
        return []
    items = []
    for label, href in SECTIONS.get(section_key, []):
        allowed = _TAB_ROLES.get(href)
        if allowed is not None and (user is None or user.role not in allowed):
            continue
        items.append((label, href))
    return items


def _nav_dropdown(label: str, href: str, sub_items: list[tuple[str, str]], is_active: bool):
    """A top-nav item that opens a hover dropdown of its section sub-pages."""
    toggle = A(
        Span(label),
        NotStr(_CARET_SVG),
        href=href,
        cls="nav-link nav-dd-toggle" + (" active" if is_active else ""),
    )
    menu = Div(
        Ul(
            *[Li(A(sub_label, href=sub_url)) for sub_label, sub_url in sub_items],
            cls="nav-dd-list",
        ),
        cls="uk-dropdown nav-dd-menu",
        uk_dropdown=(
            "mode: hover; pos: bottom-left; offset: 2; boundary-align: true; "
            "animation: uk-animation-slide-top-small; duration: 140; delay-hide: 120"
        ),
    )
    return Div(toggle, menu, cls="uk-inline nav-dd")


_HAMBURGER_SVG = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" aria-hidden="true">'
    '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/>'
    '<line x1="3" y1="18" x2="21" y2="18"/></svg>'
)
_CLOSE_SVG = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" aria-hidden="true">'
    '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>'
)


def _mobile_menu(entries):
    """Hamburger toggle + slide-down panel for narrow screens.

    Uses the checkbox-:checked CSS hack (no JS): the hidden checkbox state drives
    the panel visibility. ``entries`` is a list of
    (label, href, sub_items, is_active) — sections with sub_items render their
    sub-pages expanded and indented underneath the section title.
    """
    rows = []
    for label, href, sub_items, is_active in entries:
        if len(sub_items) > 1:
            rows.append(
                Div(
                    A(label, href=href,
                      cls="m-nav-group-title" + (" active" if is_active else "")),
                    *[A(sl, href=su, cls="m-nav-sublink") for sl, su in sub_items],
                    cls="m-nav-group",
                )
            )
        else:
            rows.append(
                A(label, href=href, cls="m-nav-link" + (" active" if is_active else ""))
            )
    return Div(
        Input(type="checkbox", id="m-nav-cb", cls="m-nav-cb"),
        Label(
            NotStr(_HAMBURGER_SVG),
            NotStr(_CLOSE_SVG),
            cls="m-nav-burger",
            aria_label="Toggle navigation menu",
            **{"for": "m-nav-cb"},
        ),
        Div(*rows, cls="m-nav-panel"),
        cls="m-nav",
    )


def TopNav(active_section: str | None = None, user=None):
    """Main navy navigation bar with role-based link filtering and avatar dropdown.

    Args:
        active_section: Key matching a section ("mc", "docs", "compass") or None for Home.
        user: Authenticated User model instance, or None for public pages (login).
              When provided, nav links are filtered by ``user.can_access(section_key)``
              and an initials avatar / dropdown is shown on the right.
    """
    desktop_items = []   # full nav with hover dropdowns (desktop only)
    mobile_entries = []  # (label, href, sub_items, is_active) for the mobile panel
    for label, href, section_key in _TOP_LINKS:
        # Filter by role — Home (section_key=None) is always visible.
        # If no user (public page like login), show all links.
        if section_key and user and not user.can_access(section_key):
            continue
        is_active = (
            (active_section == section_key)
            if section_key
            else (active_section is None and href == "/")
        )
        # Sections with sub-pages render as a hover dropdown; others stay plain.
        sub_items = _visible_sub_items(section_key, user) if section_key else []
        if len(sub_items) > 1:
            desktop_items.append(_nav_dropdown(label, href, sub_items, is_active))
        else:
            cls = "nav-link active" if is_active else "nav-link"
            desktop_items.append(A(label, href=href, cls=cls))
        mobile_entries.append((label, href, sub_items, is_active))

    # Desktop link row (hidden on mobile; replaced by the hamburger panel).
    right_items = [Div(*desktop_items, cls="nav-desktop-links")]

    # Avatar dropdown (right side of navbar) — shown on both desktop and mobile.
    if user:
        role_display = user.role.replace("_", " ").upper()
        initials = _user_initials(user.name)
        avatar = Div(
            Div(initials, cls="avatar-trigger"),
            Div(
                Div(user.name, cls="dropdown-name"),
                Div(role_display, cls="dropdown-role"),
                Hr(),
                Form(
                    Button("Sign out", type="submit", cls="sign-out-btn"),
                    method="post",
                    action="/auth/logout",
                ),
                cls="user-dropdown uk-drop uk-dropdown",
                uk_dropdown="mode: click; pos: bottom-right; offset: 8",
            ),
            cls="uk-inline",
        )
        right_items.append(avatar)

    # Mobile hamburger + slide-down panel (hidden on desktop).
    right_items.append(_mobile_menu(mobile_entries))

    # Plain nav structure (NOT MonsterUI NavBar, whose built-in mobile toggle +
    # flex-col collapse fought the custom hamburger). We control all layout via
    # .top-nav / .nav-right in theme.py.
    return Nav(
        DivLAligned(
            NotStr(_LOGO_ICON_SVG),
            H4("NCTQ.ai", cls="brand-text"),
            cls="nav-brand",
        ),
        Div(*right_items, cls="nav-right"),
        cls="top-nav",
    )


def SubNav(section: str, active_tab: str | None = None, annotation: str = "", *, user=None):
    """Section-specific tab navigation with an optional per-tab description.

    The tab row never wraps; any ``annotation`` (the page's one-line description)
    renders full-width BELOW the bar as ``.sub-nav-description`` (G4) rather than
    crowding the tab row inline. Inside the Compass section the wrapper carries
    ``sub-nav--compass`` so the active-tab underline picks up the Compass body
    teal (theme.py), while the other sections keep brand-blue chrome.

    Args:
        section: Key from SECTIONS ("mc", "docs", "compass").
        active_tab: URL path of the currently active tab.
        annotation: Optional muted description (e.g. "11713 documents indexed").
                    Rendered below the bar, full-width.
        user: Authenticated User model, or None. Tabs listed in ``_TAB_ROLES``
              are hidden unless ``user.role`` is allowed (e.g. Reports = admin).
    """
    tabs = SECTIONS.get(section, [])
    links = []
    for label, href in tabs:
        allowed = _TAB_ROLES.get(href)
        # A gated tab shows only when we have a user whose role is allowed.
        if allowed is not None and (user is None or user.role not in allowed):
            continue
        cls = "active" if href == active_tab else ""
        links.append(A(label, href=href, cls=cls))

    bar_cls = "sub-nav sub-nav--compass" if section == "compass" else "sub-nav"
    bar = Div(
        Div(Div(*links, cls="sub-nav-tabs"), cls="sub-nav-inner uk-container"),
        cls=bar_cls,
    )
    if annotation:
        # Description sits OUTSIDE the tinted bar so it reads full-width below it.
        return Div(
            bar,
            # uk-padding (not just uk-container) so its left edge matches the page
            # content container (Layout wraps content in .uk-container.uk-padding).
            P(annotation, id="sub-nav-annotation", cls="sub-nav-description uk-container uk-padding"),
            cls="sub-nav-group",
        )
    return bar

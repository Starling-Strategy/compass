"""Layout shell for the NCTQ.ai dashboard.

Wraps every page with consistent chrome: TopNav, SubNav, breadcrumb, title,
subtitle, and content — all inside a MonsterUI Container.
"""

from datetime import UTC, datetime

from fasthtml.common import A, Main, P, Span, Title
from monsterui.all import H1, Container, TextPresets

from nctqai.components.footer import Footer
from nctqai.components.nav import SubNav, TopNav


def Breadcrumb(items: list[tuple[str, str | None]]):
    """Render a breadcrumb trail.

    Args:
        items: List of (label, href) tuples. The last item is rendered bold
               with no link. All other items are rendered as links.
               Separator is " / " in muted color.
    """
    parts = []
    for i, (label, href) in enumerate(items):
        is_last = i == len(items) - 1
        if is_last:
            parts.append(Span(label, cls="font-bold"))
        else:
            parts.append(A(label, href=href))
            parts.append(Span(" / ", cls=TextPresets.muted_sm))
    return P(*parts, cls="mb-md")


def Layout(
    title: str,
    subtitle: str,
    content,
    *,
    section: str = "",
    sub_nav: str = "",
    breadcrumb: list[tuple[str, str | None]] | None = None,
    user=None,
    show_heading: bool = True,
    data_cluster: str = "",
):
    """Wrap page content in the standard NCTQ.ai layout shell.

    Args:
        title: Page heading (also used in browser tab title).
        subtitle: Description shown below the heading (muted). Omitted if empty.
        section: Active top-level section key ("mc", "docs", "compass") for nav
                 highlighting. Empty string means Home.
        sub_nav: URL of the active sub-nav tab. SubNav is only rendered when
                 section is truthy.
        breadcrumb: Optional list of (label, href) tuples for a breadcrumb
                    trail. Last item is bold, no link.
        user: Authenticated User model, or None for public pages. Passed to
              TopNav for role-based filtering and user info display.
        show_heading: When False and section is set, the H1 and subtitle are
                      suppressed; the subtitle is passed to SubNav as its
                      annotation and rendered full-width below the sub-nav bar
                      (see SubNav / .sub-nav-description, G4).
        data_cluster: When non-empty, sets ``data-cluster="..."`` on the
                      ``<body>`` element for CSS cluster scoping (e.g.
                      ``data_cluster="quality"`` scopes Scorecard-specific
                      rules via ``[data-cluster="quality"]`` selectors).
    """
    parts = []

    # 1. Top navigation bar
    parts.append(TopNav(section or None, user=user))

    # 2. Sub-navigation (only if we're in a section)
    if section:
        annotation = subtitle if (not show_heading and subtitle) else ""
        parts.append(SubNav(section, sub_nav, annotation=annotation, user=user))

    # 3-6: Container with breadcrumb, title, subtitle, content
    container_children = []

    if breadcrumb:
        container_children.append(Breadcrumb(breadcrumb))

    if show_heading:
        container_children.append(H1(title, cls="mb-md"))
        if subtitle:
            container_children.append(P(subtitle, cls=TextPresets.muted_sm))

    container_children.append(content)

    parts.append(Container(*container_children, cls="uk-padding"))

    # Site footer with the NCTQ wordmark — rendered on every page.
    parts.append(Footer(year=str(datetime.now(UTC).year)))

    main_attrs = {}
    if data_cluster:
        main_attrs["data_cluster"] = data_cluster

    return (
        Title(f"{title} — NCTQ.ai"),
        Main(*parts, **main_attrs),
    )

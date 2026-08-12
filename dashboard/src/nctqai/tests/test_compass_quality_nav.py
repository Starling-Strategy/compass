"""Nav tests for the Compass Quality Scorecard link."""
from __future__ import annotations

import fasthtml.common as fh

from nctqai.components.nav import SECTIONS, SubNav
from nctqai.models.auth import User

# Scorecard is admin-gated in the sub-nav, so rendering it needs an admin user.
_ADMIN = User(id=1, email="admin@example.com", name="Admin", role="admin")


def test_compass_sections_does_not_include_quality_cluster_heading():
    """SECTIONS["compass"] links directly to Scorecard without a QUALITY label."""
    labels = [label for label, _ in SECTIONS["compass"]]
    assert "__cluster__QUALITY" not in labels


def test_compass_sections_includes_scorecard_link():
    """SECTIONS["compass"] has a 'Scorecard' → /compass/quality/scorecard link."""
    items = {label: href for label, href in SECTIONS["compass"]}
    assert "Scorecard" in items
    assert items["Scorecard"] == "/compass/quality/scorecard"


def test_compass_sections_still_has_original_tabs():
    """SECTIONS["compass"] retains the durable top-level tabs."""
    items = {label: href for label, href in SECTIONS["compass"]}
    assert "Overview" in items
    assert "Conversations" in items
    assert "Scenarios" in items


def test_subnav_does_not_render_quality_cluster_heading():
    """SubNav does not render the old QUALITY heading before Scorecard."""
    nav = SubNav("compass", active_tab="/compass/quality/scorecard")
    html = fh.to_xml(nav)
    assert "sub-nav-cluster-heading" not in html
    assert "QUALITY" not in html


def test_subnav_active_tab_on_scorecard():
    """SubNav highlights Scorecard as active when active_tab matches."""
    nav = SubNav("compass", active_tab="/compass/quality/scorecard", user=_ADMIN)
    html = fh.to_xml(nav)
    # The active link should have class "active"
    # Find the Scorecard anchor
    assert 'href="/compass/quality/scorecard"' in html
    scorecard_idx = html.find('href="/compass/quality/scorecard"')
    # Check that the 'active' class appears nearby
    surrounding = html[max(0, scorecard_idx - 50):scorecard_idx + 80]
    assert "active" in surrounding


def test_subnav_scorecard_is_direct_link():
    """Scorecard renders as a direct tab link without a preceding cluster label."""
    nav = SubNav("compass", user=_ADMIN)
    html = fh.to_xml(nav)
    assert '<a href="/compass/quality/scorecard"' in html
    assert "QUALITY" not in html


# ── G1/G4: Compass sub-nav modifier + description-below-bar ──────────


def test_compass_subnav_carries_teal_modifier():
    """Inside Compass the wrapper gets sub-nav--compass so its active-tab
    underline picks up teal (theme.py); other sections stay plain brand-blue."""
    assert "sub-nav--compass" in fh.to_xml(SubNav("compass"))


def test_non_compass_subnav_omits_teal_modifier():
    """Metric Calculator / Documents keep the default brand-blue chrome."""
    assert "sub-nav--compass" not in fh.to_xml(SubNav("mc"))


def test_subnav_annotation_renders_below_bar_as_description():
    """G4: the annotation renders full-width BELOW the bar as
    .sub-nav-description, not inline as the old crowding .sub-nav-annotation."""
    html = fh.to_xml(SubNav("compass", annotation="247 conversations"))
    assert "sub-nav-description" in html
    assert "247 conversations" in html
    # The description sits after the .sub-nav bar in document order.
    bar_idx = html.find('class="sub-nav')
    desc_idx = html.find("sub-nav-description")
    assert bar_idx != -1 and desc_idx != -1 and bar_idx < desc_idx


def test_subnav_without_annotation_is_bare_bar():
    """No annotation → just the bar, no description block or group wrapper."""
    html = fh.to_xml(SubNav("compass"))
    assert "sub-nav-description" not in html
    assert "sub-nav-group" not in html

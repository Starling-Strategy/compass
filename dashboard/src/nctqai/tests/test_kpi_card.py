"""Tests for the KpiCard component (G2 / contract C-1).

KpiCard gained an optional ``trend`` param that renders a ``.kpi-trend`` div
AFTER the subtitle. These pure-Python tests render the component to XML and
assert the trend div appears only when ``trend=`` is passed.
"""

from __future__ import annotations

from fasthtml.common import to_xml

from nctqai.components.kpi_card import KpiCard


def test_kpi_card_renders_trend_when_passed() -> None:
    html = to_xml(KpiCard(42, "Sessions", trend="+12% vs prior period"))
    assert 'class="kpi-trend"' in html
    assert "+12% vs prior period" in html


def test_kpi_card_omits_trend_when_absent() -> None:
    html = to_xml(KpiCard(42, "Sessions"))
    assert "kpi-trend" not in html


def test_kpi_card_trend_renders_after_subtitle() -> None:
    """The trend div must follow the subtitle in document order (contract C-1)."""
    html = to_xml(
        KpiCard(42, "Sessions", subtitle="last 7 days", trend="+12% vs prior period")
    )
    sub_idx = html.find('class="kpi-subtitle"')
    trend_idx = html.find('class="kpi-trend"')
    assert sub_idx != -1 and trend_idx != -1
    assert sub_idx < trend_idx


def test_kpi_card_trend_works_on_clickable_link_variant() -> None:
    """A clickable (href) card still carries the trend div."""
    html = to_xml(KpiCard(42, "Sessions", href="/x", trend="-3% vs prior period"))
    assert 'class="kpi-trend"' in html
    assert "-3% vs prior period" in html
    assert "kpi-card--clickable" in html

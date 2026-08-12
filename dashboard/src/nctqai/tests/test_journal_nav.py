"""Nav-wiring + access tests for the Journal section."""
from __future__ import annotations

from nctqai.components.nav import SECTIONS, _TOP_LINKS
from nctqai.models.auth import User


def test_top_links_include_journal():
    assert ("Journal", "/journal", "journal") in _TOP_LINKS


def test_journal_section_has_subnav_entry():
    assert "journal" in SECTIONS


def test_admin_can_access_journal():
    assert User(id=1, email="a@x", name="A", role="admin").can_access("journal")


def test_admin_only_journal_excludes_lower_roles():
    assert not User(id=2, email="v@x", name="V", role="viewer").can_access("journal")
    assert not User(id=3, email="p@x", name="P", role="power_user").can_access("journal")
    assert not User(id=4, email="an@x", name="An", role="analyst").can_access("journal")

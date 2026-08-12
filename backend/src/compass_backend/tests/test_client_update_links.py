"""Guards for client-facing Compass testing-package links."""

from pathlib import Path


def test_client_update_docs_do_not_publish_legacy_scenario_debug_links() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    docs_dir = repo_root / "docs" / "client-updates"
    offenders: list[str] = []
    for path in docs_dir.glob("*.md"):
        text = path.read_text()
        if "staging-compass.nctq.ai/?debug=true&scenario_id=" in text:
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == []

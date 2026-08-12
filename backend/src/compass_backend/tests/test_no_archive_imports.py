from __future__ import annotations

import ast
from pathlib import Path


ACTIVE_ROOT = Path(__file__).resolve().parents[1]


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_active_compass_backend_does_not_import_archived_packages():
    forbidden_roots = {"archive", "compass_api", "compass_agents", "compass_v3", "compass"}
    offenders: list[str] = []

    for path in sorted(ACTIVE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_module_names(path):
            root = module_name.split(".", 1)[0]
            if root in forbidden_roots:
                offenders.append(f"{path.relative_to(ACTIVE_ROOT)} imports {module_name}")

    assert offenders == []

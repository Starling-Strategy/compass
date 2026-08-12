"""Guard against bare ``float()``/``int()`` coercion of a metric/profile/filter value.

The 'single numeric-value authority' doctrine (src/compass_backend/AGENTS.md
§8b) says: code under ``src/compass_backend/execution/`` must route every
metric / profile / filter value through
``parse_compass_numeric_value`` (the one parser in ``execution/_text_utils.py``),
never a bare ``float(...)`` / ``int(...)`` on a value field. The canonical parser
propagates ``None`` for unparseable input and NEVER raises, so a comma-formatted
currency value like ``"45,602"`` can never 500 the turn (that crash class is
#1772 / #1773 / #1774).

A bare ``float("45,602")`` raises ``ValueError``; a bare ``int("45,602")`` the
same. This test enumerates every ``float(x)`` / ``int(x)`` whose single argument
is an attribute access ending in ``.value`` / ``.sort_value`` / ``.filter_value``
under ``execution/`` and fails when a new one appears that is not in the
baseline. Existing justified sites are catalogued in
``fixtures/value_coercion_baseline.json`` with a ``role`` field and a one-line
justification.

To clear an entry: route the coercion through ``parse_compass_numeric_value``
(or otherwise remove the bare coercion), then delete its baseline entry in the
same PR.

To grandfather a NEW entry (rare — it must be provably safe, e.g. the argument
is ``isinstance``-guarded to ``int | float`` just above the call): add it to the
baseline with role, ``tracked_in`` justification, and reviewer sign-off.

This test is a direct port of ``tests/test_no_prose_dispatch.py``'s mechanism
(AST walk + JSON baseline + a stale-entry check + a scanner self-test on
synthetic source).

Run:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_no_bare_value_coercion.py
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE_ROOT = ROOT / "src" / "compass_backend"
SCAN_DIRS = ("execution",)
BASELINE_PATH = pathlib.Path(__file__).parent / "fixtures" / "value_coercion_baseline.json"

# Coercions we forbid and the attribute suffixes that name a metric/profile/
# filter value. A bare float()/int() on any of these raises on a comma-formatted
# string ("45,602") — the #1772 crash class.
_COERCION_FUNCS = {"float", "int"}
_VALUE_SUFFIXES = ("value", "sort_value", "filter_value")


def _attr_chain_suffix(node: ast.AST) -> str | None:
    """Return the trailing attribute name if ``node`` is an attribute access.

    ``float(row.value)`` -> ``"value"``; ``float(a.b.sort_value)`` ->
    ``"sort_value"``; ``float(value)`` (a bare Name) -> ``None``; any non-
    attribute argument (call, subscript, literal) -> ``None``.
    """

    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def scan_tree(tree: ast.AST, rel_path: str) -> list[dict[str, Any]]:
    """Return bare-value-coercion findings for one parsed AST. Pure function."""

    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id in _COERCION_FUNCS):
            continue
        # Single positional argument, no keywords/starargs — anything else is
        # not the bare ``float(x.value)`` shape we forbid.
        if len(node.args) != 1 or node.keywords:
            continue
        suffix = _attr_chain_suffix(node.args[0])
        if suffix is None or suffix not in _VALUE_SUFFIXES:
            continue
        findings.append({
            "kind": "bare_value_coercion",
            "file": rel_path,
            "func": node.func.id,
            "attr": suffix,
            "lineno": node.lineno,
        })
    return findings


def scan_file(path: pathlib.Path) -> list[dict[str, Any]]:
    """Return bare-value-coercion findings in one Python file via AST walk."""

    tree = ast.parse(path.read_text())
    return scan_tree(tree, str(path.relative_to(ROOT)))


def scan_all() -> list[dict[str, Any]]:
    """Scan every Python file under the target directories."""

    findings: list[dict[str, Any]] = []
    for sub in SCAN_DIRS:
        sub_root = PACKAGE_ROOT / sub
        if not sub_root.exists():
            continue
        for path in sorted(sub_root.rglob("*.py")):
            if "/tests/" in str(path):
                continue
            findings.extend(scan_file(path))
    return findings


def _match_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    """Stable key matching scan findings against baseline entries.

    Uses (file, func, attr) so the test is robust to line-number changes when a
    file is edited above the coercion site."""

    return (entry["file"], entry["func"], entry["attr"])


def test_no_new_bare_value_coercion_below_execution() -> None:
    """Fail when a new bare float()/int() on a value field appears unbaselined.

    Doctrine reference: 'Numeric value coercion' (§8b) in
    src/compass_backend/AGENTS.md.
    """

    baseline = json.loads(BASELINE_PATH.read_text())
    baseline_keys = {_match_key(e) for e in baseline["entries"]}
    current = scan_all()
    current_keys = {_match_key(e) for e in current}

    new_keys = current_keys - baseline_keys
    new_findings = [e for e in current if _match_key(e) in new_keys]

    if new_findings:
        lines = [
            "",
            "New bare value-coercion sites detected under execution/.",
            "See src/compass_backend/AGENTS.md §8b 'Numeric value coercion'.",
            "",
            "Each finding must either be routed through",
            "parse_compass_numeric_value (execution/_text_utils.py) or added to:",
            f"  {BASELINE_PATH.relative_to(ROOT)}",
            "with role + tracked_in justification and reviewer sign-off.",
            "",
            "New findings:",
        ]
        for finding in new_findings:
            lines.append(
                f"  {finding['file']}:{finding['lineno']}: "
                f"{finding['func']}(...{finding['attr']})"
            )
        pytest.fail("\n".join(lines))


def test_baseline_does_not_contain_stale_entries() -> None:
    """Fail when a baselined entry has been removed from source but not the baseline.

    Catches the case where a coercion site was fixed (routed through the parser)
    in code but the baseline JSON was not updated in the same PR — keeps the
    baseline honest as sites are cleared.
    """

    baseline = json.loads(BASELINE_PATH.read_text())
    baseline_keys = {_match_key(e) for e in baseline["entries"]}
    current_keys = {_match_key(e) for e in scan_all()}

    stale_keys = baseline_keys - current_keys
    stale_entries = [e for e in baseline["entries"] if _match_key(e) in stale_keys]

    if stale_entries:
        lines = [
            "",
            "Baseline contains entries that no longer appear in source. Either:",
            "  - The coercion was routed through the parser and the baseline JSON",
            "    needs the entry removed, or",
            "  - The file/func/attr changed and the baseline needs updating.",
            "",
            "Stale entries:",
        ]
        for entry in stale_entries:
            lines.append(
                f"  {entry['file']}: {entry['func']}(...{entry['attr']}) "
                f"role={entry.get('role')}"
            )
        pytest.fail("\n".join(lines))


def test_scanner_detects_bare_value_coercion_via_ast() -> None:
    """Prove the detection logic works by feeding it a synthetic source.

    Without this wiring check, an AST-walk regression that silently matched zero
    sites would still let test_no_new_bare_value_coercion... pass (empty minus
    empty equals empty). This parses a known-bad source in memory and confirms
    each forbidden shape produces a finding while safe shapes do not.
    """

    synthetic_source = """
def bad_float(row):
    return float(row.value)

def bad_int(spec):
    return int(spec.filter_value)

def bad_chained(a):
    return float(a.b.sort_value)

def safe_bare_name(value):
    return float(value)

def safe_parsed(match):
    return float(match.group(1).replace(",", ""))

def safe_non_value_attr(row):
    return int(row.district_id)
"""

    tree = ast.parse(synthetic_source)
    findings = scan_tree(tree, "synthetic/test_only.py")
    matched = {(f["func"], f["attr"]) for f in findings}

    assert ("float", "value") in matched, f"missed float(row.value): {findings}"
    assert ("int", "filter_value") in matched, f"missed int(spec.filter_value): {findings}"
    assert ("float", "sort_value") in matched, f"missed chained sort_value: {findings}"
    # Safe shapes must NOT be flagged.
    assert len(findings) == 3, f"flagged a safe shape: {findings}"


def test_baseline_entries_have_required_fields() -> None:
    """Every baseline entry must have file, func, attr, role, and tracked_in."""

    baseline = json.loads(BASELINE_PATH.read_text())
    required_fields = ("file", "func", "attr", "role", "tracked_in")
    valid_roles = set(baseline["roles"].keys())

    invalid: list[str] = []
    for entry in baseline["entries"]:
        missing = [f for f in required_fields if f not in entry or entry[f] in (None, "")]
        if missing:
            invalid.append(f"  {entry.get('file', '<unknown>')} missing fields: {missing}")
        elif entry["role"] not in valid_roles:
            invalid.append(
                f"  {entry.get('file', '<unknown>')} role={entry['role']!r} "
                f"not in {sorted(valid_roles)}"
            )

    if invalid:
        pytest.fail("Baseline schema violations:\n" + "\n".join(invalid))

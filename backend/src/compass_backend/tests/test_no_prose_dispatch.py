"""Guard against new prose-dispatch patterns below the planner.

The 'No prose-dispatch below the planner' doctrine (CLAUDE.md, sibling to the
critic deterministic pre-check pattern) says: code under
``src/compass_backend/{execution,catalog,orchestration}/`` must not read
``plan.question`` / ``deps.message`` / any free-text field with regex or
phrase-set inspection to ROUTE a request, REWRITE an operation, or SUBSTITUTE
a typed field.

Pattern matching is allowed only in the roles documented by
``docs/no-prose-dispatch.md`` and enumerated in the baseline role registry:
typed output validators, selected-guidance validators, prompt-context
selection before planner output, candidate broadening, typed referent
resolution, data-value filtering, canonicalization, and rendering false
positives.

This test enumerates every existing pattern via AST walk and fails CI when
a new pattern appears that is not in the baseline. Existing patterns are
catalogued in ``fixtures/prose_dispatch_baseline.json`` with a ``role`` field
and a one-line justification.

To clear an entry: delete the heuristic, then remove its baseline entry in
the same PR.

To grandfather a NEW entry (rare — it must match the doctrine):
add it to the baseline with role, ``tracked_in`` justification, and reviewer
sign-off.

Run:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_no_prose_dispatch.py
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PACKAGE_ROOT = ROOT / "src" / "compass_backend"
SCAN_DIRS = ("execution", "catalog", "orchestration", "planning", "rendering")
BASELINE_PATH = pathlib.Path(__file__).parent / "fixtures" / "prose_dispatch_baseline.json"


def _is_multi_word(value: str) -> bool:
    stripped = value.strip()
    return " " in stripped and len(stripped) > 2


def _extract_string_elts(node: ast.AST) -> list[str]:
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def _container_from_value(node: ast.AST) -> ast.AST | None:
    """Return the set/list/tuple inside a frozenset(...) wrapper or the node itself."""

    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return node
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and node.args
        and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))
    ):
        return node.args[0]
    return None


def _regex_word_tokens(pattern_src: str) -> list[str]:
    """Strip regex syntax and return remaining English-word tokens (>=3 chars)."""

    cleaned = re.sub(r"\\[a-zA-Z]", " ", pattern_src)
    cleaned = re.sub(r"[\\()\[\]{}|^$+*?:,.0-9]", " ", cleaned)
    return [w for w in cleaned.split() if len(w) >= 3 and w.isalpha()]


def scan_tree(tree: ast.AST, rel_path: str) -> list[dict[str, Any]]:
    """Return prose-dispatch findings for one parsed AST. Pure function for testing."""

    findings: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        # 1. Inline tuple/list/set on the right-hand side of `for x in (...)`
        if isinstance(node, ast.comprehension):
            container = _container_from_value(node.iter)
            if container is not None:
                strings = _extract_string_elts(container)
                multi_word = [s for s in strings if _is_multi_word(s)]
                if multi_word:
                    sorted_mw = sorted(multi_word)
                    findings.append({
                        "kind": "inline_phrase_iterable",
                        "file": rel_path,
                        "name": None,
                        "first_sample": sorted_mw[0],
                    })
        # 2. Inline tuple/list/set on the right-hand side of `x in (...)`
        if isinstance(node, ast.Compare) and node.ops:
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                container = _container_from_value(comparator)
                if container is None:
                    continue
                strings = _extract_string_elts(container)
                multi_word = [s for s in strings if _is_multi_word(s)]
                if multi_word:
                    sorted_mw = sorted(multi_word)
                    findings.append({
                        "kind": "inline_in_compare",
                        "file": rel_path,
                        "name": None,
                        "first_sample": sorted_mw[0],
                    })
        # 3. Named constants assigned to set/list/tuple/frozenset(...) literals
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not target_names:
                continue
            container = _container_from_value(node.value)
            if container is None:
                continue
            strings = _extract_string_elts(container)
            multi_word = [s for s in strings if _is_multi_word(s)]
            if not multi_word:
                continue
            sorted_mw = sorted(multi_word)
            findings.append({
                "kind": "phrase_set",
                "file": rel_path,
                "name": target_names[0],
                "first_sample": sorted_mw[0],
            })
        # 4. Annotated assignments: `name: type = set/list/tuple/frozenset(...)` literals
        #    ast.AnnAssign.target is a single Name (not a list like ast.Assign.targets).
        #    ast.AnnAssign.value may be None for bare declarations like `x: int`.
        if isinstance(node, ast.AnnAssign):
            target_name = node.target.id if isinstance(node.target, ast.Name) else None
            if not target_name or node.value is None:
                continue
            container = _container_from_value(node.value)
            if container is None:
                continue
            strings = _extract_string_elts(container)
            multi_word = [s for s in strings if _is_multi_word(s)]
            if not multi_word:
                continue
            sorted_mw = sorted(multi_word)
            findings.append({
                "kind": "phrase_set",
                "file": rel_path,
                "name": target_name,
                "first_sample": sorted_mw[0],
            })
        # 5. re.compile(r"...prose...") patterns
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            words = _regex_word_tokens(node.args[0].value)
            if len(words) >= 2:
                sorted_words = sorted(set(words))
                findings.append({
                    "kind": "re_compile_prose",
                    "file": rel_path,
                    "name": None,
                    "first_sample": sorted_words[0],
                })

    return findings


def scan_file(path: pathlib.Path) -> list[dict[str, Any]]:
    """Return prose-dispatch findings in one Python file via AST walk."""

    tree = ast.parse(path.read_text())
    return scan_tree(tree, str(path.relative_to(ROOT)))


def scan_all() -> list[dict[str, Any]]:
    """Scan every Python file under the three target directories."""

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


def _match_key(entry: dict[str, Any]) -> tuple[str, str | None, str]:
    """Stable key for matching scan findings against baseline entries.

    Uses (file, name, first_sample) so the test is robust to reordering
    inside a phrase set and to line-number changes."""

    return (entry["file"], entry.get("name"), entry["first_sample"])


def test_no_new_prose_dispatch_patterns_below_planner() -> None:
    """Fail when a new prose-dispatch pattern appears that isn't grandfathered.

    Doctrine reference: the 'No prose dispatch below the planning boundary'
    guardrail in src/compass_backend/AGENTS.md (guardrail 4).
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
            "New prose-dispatch patterns detected below the planner.",
            "See CLAUDE.md 'No prose-dispatch below the planner' for the doctrine.",
            "",
            "Each finding must either be refactored (preferred — see the",
            "allowed-role doctrine in CLAUDE.md / docs/no-prose-dispatch.md) "
            + "or added to:",
            f"  {BASELINE_PATH.relative_to(ROOT)}",
            "with role + tracked_in justification and reviewer sign-off.",
            "",
            "New findings:",
        ]
        for finding in new_findings:
            name = finding.get("name") or "<inline>"
            lines.append(
                f"  {finding['file']}: {finding['kind']} name={name} first_sample={finding['first_sample']!r}"
            )
        pytest.fail("\n".join(lines))


def test_baseline_does_not_contain_stale_entries() -> None:
    """Fail when a baselined entry has been deleted from source but not the baseline.

    This catches the case where a 'deletable' item was removed in code but
    the baseline JSON was not updated in the same PR — keeps the baseline
    honest as Bucket A items ship.

    Entries with a ``"scanner_gap"`` field are exempted from the staleness
    check — they document patterns the AST scanner cannot yet detect (e.g.,
    annotated assignments using ``ast.AnnAssign`` syntax).  The scanner
    extension that would make them visible is tracked as a follow-up PR.
    """

    baseline = json.loads(BASELINE_PATH.read_text())
    # Entries with scanner_gap are documentation-only; the scanner cannot find
    # them, so they must not participate in the stale-entry comparison.
    checkable_entries = [e for e in baseline["entries"] if not e.get("scanner_gap")]
    baseline_keys = {_match_key(e) for e in checkable_entries}
    current_keys = {_match_key(e) for e in scan_all()}

    stale_keys = baseline_keys - current_keys
    stale_entries = [e for e in checkable_entries if _match_key(e) in stale_keys]

    if stale_entries:
        lines = [
            "",
            "Baseline contains entries that no longer appear in source. Either:",
            "  - The heuristic was deleted and the baseline JSON needs updating, or",
            "  - The heuristic was renamed and the baseline first_sample / name needs updating.",
            "",
            "Stale entries:",
        ]
        for entry in stale_entries:
            name = entry.get("name") or "<inline>"
            lines.append(
                f"  {entry['file']}: name={name} first_sample={entry['first_sample']!r} role={entry.get('role')}"
            )
        pytest.fail("\n".join(lines))


def test_scanner_detects_each_prose_dispatch_shape_via_ast() -> None:
    """Prove the detection logic actually works by feeding it a synthetic source.

    Without this wiring check, an AST-walk regression that silently matched
    zero patterns would still let test_no_new_prose_dispatch... pass (empty
    minus empty equals empty). This test parses a known-bad Python source
    in memory and confirms each pattern shape produces a finding.
    """

    synthetic_source = """
import re

_NEW_BAD_PHRASES = {"silly walk", "ministry of funny"}

_NEW_BAD_RE = re.compile(r"how many ministries are silly today")

def check(message: str) -> bool:
    return any(p in message for p in ("ministry of silly", "ministry of funny"))

def check_compare(message: str) -> bool:
    return message in {"ministry of silly", "ministry of funny"}
"""

    tree = ast.parse(synthetic_source)
    findings = scan_tree(tree, "synthetic/test_only.py")
    kinds = {f["kind"] for f in findings}

    assert "phrase_set" in kinds, f"missed module-level phrase set: {findings}"
    assert "inline_phrase_iterable" in kinds, f"missed comprehension iterable: {findings}"
    assert "inline_in_compare" in kinds, f"missed 'x in {{...}}' compare: {findings}"
    assert "re_compile_prose" in kinds, f"missed re.compile prose: {findings}"


def test_baseline_entries_have_required_fields() -> None:
    """Every baseline entry must have file, role, and tracked_in (justification)."""

    baseline = json.loads(BASELINE_PATH.read_text())
    required_fields = ("file", "kind", "first_sample", "role", "tracked_in")
    valid_roles = set(baseline["roles"].keys())

    invalid: list[str] = []
    for entry in baseline["entries"]:
        missing = [f for f in required_fields if f not in entry or entry[f] in (None, "")]
        if missing:
            invalid.append(
                f"  {entry.get('file','<unknown>')} missing fields: {missing}"
            )
        elif entry["role"] not in valid_roles:
            invalid.append(
                f"  {entry.get('file','<unknown>')} role={entry['role']!r} not in {sorted(valid_roles)}"
            )

    if invalid:
        pytest.fail(
            "Baseline schema violations:\n" + "\n".join(invalid)
        )

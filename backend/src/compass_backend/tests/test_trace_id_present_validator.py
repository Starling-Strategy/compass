"""Tests for the `trace_id_present` observability validator (sweep-PR-3).

Two layers of coverage:

1. **Behavioral**: the validator pass/fails correctly on populated /
   empty / None / whitespace-only trace_id values.

2. **Sync-with-migration**: the seeded compass.criteria row from
   migration `035_seed_observability_criteria.sql` references
   `validator_name='trace_id_present'`, which must be registered in
   the in-code validator registry. The pair is checked together so a
   future code rename without a migration update (or vice versa)
   fails CI loudly rather than silently leaving the criterion
   unfireable.

3. **Static invariant**: every `_insert_message` call in
   `src/compass_backend/session/postgres.py` passes `trace_id=`
   explicitly. Guards against a future save path that drops the
   trace_id silently — the failure mode `trace_id_present` exists to
   surface, but at the *runtime* layer rather than the database layer.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.validators.registry import known_validators, lookup
from compass_backend.tests.conftest import make_evaluator_context


MIGRATION_PATH = (
    Path("src/compass_data_sync/migrations/035_seed_observability_criteria.sql")
)
POSTGRES_STORE_PATH = Path("src/compass_backend/session/postgres.py")


def _ctx(trace_id: str | None) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="11111111-1111-1111-1111-111111111111",
        turn_index=0,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_id_present_passes_on_real_id() -> None:
    fn = lookup("trace_id_present")
    assert fn is not None, "trace_id_present must be registered"
    out = await fn(_ctx("019e3ac6f598310cb74abb361b7d"), {})
    assert out.outcome == "pass"
    assert "trace_id present" in out.reason


@pytest.mark.asyncio
async def test_trace_id_present_fails_on_none() -> None:
    fn = lookup("trace_id_present")
    assert fn is not None
    out = await fn(_ctx(None), {})
    assert out.outcome == "fail"
    assert "trace_id absent" in out.reason


@pytest.mark.asyncio
async def test_trace_id_present_fails_on_empty_string() -> None:
    fn = lookup("trace_id_present")
    assert fn is not None
    out = await fn(_ctx(""), {})
    assert out.outcome == "fail"


@pytest.mark.asyncio
async def test_trace_id_present_fails_on_whitespace_only() -> None:
    """Whitespace-only trace_ids are functionally absent — Logfire
    cannot resolve them. The validator strips before deciding."""
    fn = lookup("trace_id_present")
    assert fn is not None
    out = await fn(_ctx("   "), {})
    assert out.outcome == "fail"


# ---------------------------------------------------------------------------
# Registration drift-detection
# ---------------------------------------------------------------------------


def test_trace_id_present_is_registered() -> None:
    """The seed migration depends on this name. Don't rename without
    updating migration 035."""
    assert "trace_id_present" in known_validators()


def test_migration_035_seeds_trace_id_present() -> None:
    """Sync-with-migration: the SQL must seed a row whose
    `payload.validator_name` matches the registered validator name."""
    sql = MIGRATION_PATH.read_text()
    assert "'trace_id_present'" in sql, (
        "expected criterion_code 'trace_id_present' in migration 035"
    )
    assert '"validator_name": "trace_id_present"' in sql, (
        "expected payload.validator_name='trace_id_present' in migration 035"
    )
    # The category column must use the new 'observability' value so the
    # Quality dashboard / analyze_sweep.py can carve it out as a
    # third orthogonal axis alongside accuracy and process_integrity.
    assert "'observability'" in sql, (
        "expected category='observability' in migration 035"
    )
    # Mandatory-global so every turn evaluates it.
    assert "TRUE" in sql  # is_mandatory_global = TRUE
    assert "trace-id-present-v1" in sql, "expected versioned version_hash"


# ---------------------------------------------------------------------------
# Static invariant — every assistant-row save call must pass trace_id
# ---------------------------------------------------------------------------


def test_postgres_store_insert_message_calls_pass_trace_id_explicitly() -> None:
    """Every `_insert_message(...)` call in PostgresSessionStore must pass
    `trace_id=` as a keyword argument.

    Guards the runtime invariant that produces the verdict `trace_id_present`
    fails on. If a future save path forgets to thread trace_id through,
    this test fails CI before the regression makes it to staging.
    """
    source = POSTGRES_STORE_PATH.read_text()
    tree = ast.parse(source)
    missing: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match `<expr>._insert_message(...)`
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "_insert_message"
        ):
            continue
        keyword_names = {kw.arg for kw in node.keywords}
        if "trace_id" not in keyword_names:
            # Find the line for a helpful failure message.
            snippet = ast.unparse(node).splitlines()[0][:120]
            missing.append((node.lineno, snippet))
    assert not missing, (
        "every _insert_message call must pass trace_id= explicitly. "
        f"Found {len(missing)} call(s) missing trace_id:\n"
        + "\n".join(f"  line {lineno}: {snippet}" for lineno, snippet in missing)
    )


def test_postgres_store_insert_message_signature_requires_trace_id() -> None:
    """The `_insert_message` definition itself must keep `trace_id` as
    a non-default keyword-only argument so callers can't silently omit it.

    If someone makes `trace_id: str | None = None` (default-ed), the
    static call-site check above would still pass but callers gain the
    freedom to silently drop the trace. The signature check below
    pins both invariants together.
    """
    source = POSTGRES_STORE_PATH.read_text()
    # Grab the def + parameter list. The function uses keyword-only args
    # (* before the keyword block). Confirm trace_id is keyword-only and
    # has no default value (= literal).
    match = re.search(
        r"async\s+def\s+_insert_message\s*\(([^)]*)\)",
        source,
        flags=re.DOTALL,
    )
    assert match is not None, "could not locate _insert_message in postgres.py"
    sig = match.group(1)
    # Must declare trace_id parameter.
    assert "trace_id" in sig, "trace_id missing from _insert_message signature"
    # No default for trace_id (no `trace_id: ... = None`).
    assert not re.search(
        r"trace_id\s*:\s*[^,)=]+\s*=\s*None", sig
    ), (
        "trace_id parameter must NOT default to None on _insert_message — "
        "callers must thread it through explicitly so a missing trace_id "
        "surfaces at the call site, not silently in the persisted row."
    )

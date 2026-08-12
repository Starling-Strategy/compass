"""The 500 path must log the full exception + traceback, not just the sanitized
error string returned to the client.

Observability regression: the swallowed traceback in `_build_500_for_exc` is why
a one-line `ValueError` (the #1772 currency-parse crash) took a multi-cycle hunt
to (not even) locate — Logfire/stdout only ever saw the error *string*, never the
frame. This test pins that the original exception, with its traceback, is recorded
(keyed by trace_id) while the client still gets the opaque 500.
"""

from __future__ import annotations

import logging

from compass_backend.api.chat import _build_500_for_exc


def test_build_500_logs_exception_with_traceback(caplog):
    captured: ValueError
    try:
        raise ValueError("could not convert string to float: '45,602'")
    except ValueError as exc:
        exc.trace_id = "trace-abc"  # mimic the orchestrator-attached id
        captured = exc  # `exc` is auto-deleted at end of except; keep a handle
        with caplog.at_level(logging.ERROR, logger="compass_backend.api.chat"):
            http_exc = _build_500_for_exc(exc)

    # Client response stays sanitized (no leak of internals).
    assert http_exc.status_code == 500
    assert http_exc.detail["error"] == "internal_error"

    # But the server recorded the full exception + traceback, with binding context.
    err_records = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info
    ]
    assert err_records, "the 500 path must log an ERROR with exc_info (traceback)"
    rec = err_records[0]
    assert rec.exc_info[1] is captured  # the real exception object…
    assert rec.exc_info[2] is not None  # …carrying its traceback frames
    assert getattr(rec, "trace_id", None) == "trace-abc"
    assert "45,602" in caplog.text

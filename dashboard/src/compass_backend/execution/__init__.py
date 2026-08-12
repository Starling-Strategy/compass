"""Execution helpers — vendored slice for the standalone dashboard.

Only execution._text_utils (stdlib-only string helpers, e.g. truncate) is
imported by the dashboard's vendored quality.scorecard. The canonical
execution/__init__.py in the Compass API monorepo imports catalog/executor/
types, none of which are vendored here — so this is a deliberate STUB. Do not
replace it with main's __init__.py or add sibling imports; that lights up a
ModuleNotFoundError at container boot. (See the db/ and quality/ stubs.)
"""

__all__ = []

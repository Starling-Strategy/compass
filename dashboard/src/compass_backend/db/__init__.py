"""Database adapters — vendored slice for the standalone dashboard.

Only db.scorecard and db.builds are imported by the dashboard's scorecard
loader. Sibling repos ship with the Compass API monorepo and are NOT vendored
here — do not add sibling imports.
"""

__all__ = []

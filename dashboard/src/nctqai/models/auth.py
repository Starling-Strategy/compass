"""Auth models — User with role-based permission checks.

`Role` is the single source of truth for the dashboard role list. It mirrors
the CHECK constraint in migrations/001_auth_tables.sql; that migration is
append-only and already applied, so this enum is the *runtime* authority every
Python caller derives from (admin.VALID_ROLES, the section/tab maps below).
test_role_gating.test_role_enum_matches_db_constraint guards them from drifting.
"""

from enum import StrEnum

from pydantic import BaseModel
from datetime import datetime


class Role(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    POWER_USER = "power_user"
    ADMIN = "admin"


class User(BaseModel):
    """User record from nctqai.users."""
    id: int
    email: str
    name: str
    role: str  # one of Role; kept as str because psycopg2 returns a str
    is_active: bool = True
    last_login: datetime | None = None

    def can_access(self, section: str) -> bool:
        """Check if user role can access a dashboard section."""
        section_roles = {
            "mc": (Role.VIEWER, Role.ANALYST, Role.POWER_USER, Role.ADMIN),
            "docs": (Role.VIEWER, Role.ANALYST, Role.POWER_USER, Role.ADMIN),
            "journal": (Role.ADMIN,),  # admin-only: the Compass Journal feed
            "pa": (Role.POWER_USER, Role.ADMIN),
            # Compass conversation-monitoring surface is open to ALL roles
            # (#1806): Overview, Conversations + detail, Flagged Issues, and
            # Data Universe. The eval-BUILDER tabs (Scenarios, Scorecard) stay
            # admin-only via their own admin=True route gates + _TAB_ROLES, not
            # this section map.
            "compass": (Role.VIEWER, Role.ANALYST, Role.POWER_USER, Role.ADMIN),
            "evals": (Role.POWER_USER, Role.ADMIN),
            "admin": (Role.ADMIN,),
        }
        allowed = section_roles.get(section, ())
        return self.role in allowed

    def can_review(self) -> bool:
        """Check if user can take review actions (accept/reject)."""
        return self.role in (Role.ANALYST, Role.POWER_USER, Role.ADMIN)

    def can_hold(self) -> bool:
        """Check if user can place/release answer holds (supervisory action)."""
        return self.role in (Role.POWER_USER, Role.ADMIN)

    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

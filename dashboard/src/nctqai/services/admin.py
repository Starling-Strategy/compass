"""Admin service — user CRUD operations."""

import logging

import psycopg2

from nctqai.db import run_sql, run_sql_write
from nctqai.services.auth import destroy_user_sessions

logger = logging.getLogger(__name__)


def get_all_users() -> list[dict]:
    """Get all users for the admin panel."""
    return run_sql(
        "SELECT id, email, name, role, is_active, created_at, last_login "
        "FROM nctqai.users ORDER BY name"
    )


def create_user(email: str, name: str, role: str, created_by: int) -> bool:
    """Create a new user (invite). Returns True on duplicate-email or success.

    Only swallows IntegrityError (the email already exists) — every other
    exception (DB outage, auth failure, schema drift) propagates so the
    admin sees a real 500 instead of a confusing "Email may already exist".
    """
    try:
        run_sql_write(
            "INSERT INTO nctqai.users (email, name, role, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (email.lower().strip(), name.strip(), role, created_by),
        )
        return True
    except psycopg2.errors.UniqueViolation:
        logger.info("create_user: duplicate email %s", email)
        return False


def update_user(user_id: int, role: str | None = None, is_active: bool | None = None):
    """Update user role and/or active status."""
    if role is not None:
        run_sql_write(
            "UPDATE nctqai.users SET role = %s WHERE id = %s",
            (role, user_id),
        )
    if is_active is not None:
        run_sql_write(
            "UPDATE nctqai.users SET is_active = %s WHERE id = %s",
            (is_active, user_id),
        )
        if not is_active:
            destroy_user_sessions(user_id)


def count_active_admins() -> int:
    """Count active admin users."""
    rows = run_sql(
        "SELECT COUNT(*) as cnt FROM nctqai.users "
        "WHERE role = 'admin' AND is_active = true"
    )
    return rows[0]["cnt"] if rows else 0

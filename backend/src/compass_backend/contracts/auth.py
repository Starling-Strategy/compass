"""Authentication contracts shared across Compass layers.

`AuthenticatedUser` lives here (not `api/auth.py`) so non-api layers
(`orchestration/`, future `quality/` callers) can type-hint their
parameters without taking an upward import on the transport layer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuthenticatedUser(BaseModel):
    """Authenticated caller admitted by the fresh transport layer."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    name: str | None = None
    auth_method: Literal["api_key"] = "api_key"
    api_key_id: str | None = Field(default=None, min_length=1)
    is_admin: bool = False

    @property
    def owner_email(self) -> str:
        """Stable session owner identity for this caller."""

        if self.email:
            return self.email
        return f"api_key:{self.api_key_id or 'unknown'}"


__all__ = ["AuthenticatedUser"]

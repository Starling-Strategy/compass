"""Service layer between the API transport and the downstream domain layers.

Owns chat-turn orchestration today (`build_chat_response`). Sits between
`api/` (FastAPI routes, SSE, auth) and the domain packages (planning,
execution, quality, rendering, session). Routes call into this package;
**this package does not import from `api/` at all** — `AuthenticatedUser`
moved to `compass_backend.contracts.auth` so it can be referenced without
an upward import.
"""

from compass_backend.orchestration.chat import build_chat_response

__all__ = ["build_chat_response"]

"""HTTP middleware and exception handlers."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from compass_backend.observability import log_debug, log_warn  # noqa: F401  # log_warn is imported so tests can assert it is NOT called for auth errors


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Keep HTTPException responses consistent across route modules."""

    if exc.status_code in (401, 403):
        # token_shape inference leaks JWT-vs-opaque structure to production logs.
        # Demote to debug so the diagnostic stays available locally without
        # surfacing on every failed auth in prod.
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else ""
        token_shape = "jwt" if token.count(".") == 2 else "opaque" if token else "none"
        log_debug(
            "Auth error",
            status_code=exc.status_code,
            detail=str(exc.detail),
            token_shape=token_shape,
            path=request.url.path,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


def install_transport_middleware(
    app: FastAPI,
    *,
    cors_origins: Sequence[str],
    environment: str,
) -> None:
    """Install middleware and exception handlers for the API boundary."""

    app.add_exception_handler(HTTPException, http_exception_handler)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'self' "
            "https://nctq.org https://*.nctq.org "
            "https://nctq.ai https://*.nctq.ai"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), interest-cohort=()"
        )
        if environment != "development":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

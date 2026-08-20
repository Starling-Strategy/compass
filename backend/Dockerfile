# Compass Agent API Dockerfile
# Production (Azure Container App) — connects to PostgreSQL directly via NAT
# Gateway. NO proxychains, NO Tailscale, NO httpx[socks].
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package installation (pinned for reproducibility)
RUN pip install uv==0.9.15

# Copy dependency files first (for layer caching)
COPY pyproject.toml uv.lock README.md ./
COPY src/compass_backend/ src/compass_backend/
COPY content/ content/
COPY scripts/entrypoint.sh scripts/entrypoint.sh

# Install from lockfile for reproducible builds.
# uv sync --frozen refuses to update the lockfile; --no-dev skips dev-only deps;
# --no-install-project skips installing the package itself (we run from src
# directly via PYTHONPATH).
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# Create data directory + make entrypoint executable
RUN mkdir -p data && chmod +x scripts/entrypoint.sh

# Non-root user for security (matching Dockerfile.dashboard)
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port — note: NO USE_PROXYCHAINS env var; production reaches PG directly.
ENV PORT=8000 \
    PYTHONPATH=/app/src
EXPOSE 8000

# Container liveness only. `/api/v1/ready` intentionally includes a DB probe,
# but using it for Docker health can drain every API container during transient
# staging DB/Tailscale pressure and make Traefik return "no available server".
# Keep readiness available for explicit checks; keep Docker focused on process
# liveness.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

CMD ["scripts/entrypoint.sh"]

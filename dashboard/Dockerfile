# NCTQ.ai Dashboard — Production/Staging Dockerfile
# FastHTML + MonsterUI + uvicorn
#
# Build: docker build -t nctq-piper .
# Run:   docker run -p 5001:5001 --env-file .env nctq-piper

FROM python:3.12-slim

# System deps: psycopg2 needs libpq
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache friendly)
# Uses dashboard-only requirements — no torch/docling (~4GB savings)
COPY requirements-dashboard.txt .
RUN pip install --no-cache-dir -r requirements-dashboard.txt

# Copy source
COPY src/ ./src/
COPY pyproject.toml .

# Non-root user for security
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

# Runtime config
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
EXPOSE 5001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:5001/health || exit 1

CMD ["uvicorn", "nctqai.main:app", "--host", "0.0.0.0", "--port", "5001"]

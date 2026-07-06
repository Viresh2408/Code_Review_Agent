# syntax=docker/dockerfile:1

# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy dependency manifest first (layer cache)
COPY pyproject.toml .

# Install into a prefix we can copy to the final stage
RUN uv pip install --system --no-cache-dir -r pyproject.toml 2>/dev/null || \
    uv pip install --system --no-cache-dir \
        fastapi \
        "uvicorn[standard]" \
        "celery[redis]" \
        "sqlalchemy[asyncio]" \
        asyncpg \
        alembic \
        pydantic-settings \
        pygithub \
        httpx \
        structlog \
        python-dotenv \
        chromadb \
        redis \
        prometheus-client \
        prometheus-fastapi-instrumentator

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source
COPY . .

# Secrets directory (mounted at runtime, never baked in)
RUN mkdir -p /app/secrets && chown appuser:appgroup /app/secrets

USER appuser

EXPOSE 8000

# Default: run API server (overridden by docker-compose for the worker service)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

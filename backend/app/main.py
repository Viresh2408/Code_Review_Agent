"""
FastAPI application entry point.

Routes:
  GET  /health              — liveness probe
  POST /webhooks/github     — GitHub App webhook receiver (FR-1)
"""

from __future__ import annotations

import json
import logging

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.webhooks.github import router as github_router
from app.api.v1.endpoints import router as api_v1_router
from app.observability.logging_config import setup_logging
from prometheus_fastapi_instrumentator import Instrumentator

# Initialize unified structured logging config
setup_logging()

settings = get_settings()
logger = structlog.get_logger(__name__)

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Code Review Agent API",
    description="AI-powered code review & technical debt tracking.",
    version="0.1.0",
    # Disable docs in production to reduce attack surface
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url="/redoc" if settings.app_env != "production" else None,
)

# Mount Prometheus metrics instrumentator
Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("backend_starting", env=settings.app_env, log_level=settings.log_level)
    # Zero-ops SQLite fallback: auto-create tables on startup in development
    if "sqlite" in settings.database_url:
        from app.db.base import Base
        from app.db.session import async_engine
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("sqlite_tables_created_successfully")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("backend_shutting_down")


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    """Liveness probe — used by Docker healthcheck and load balancers."""
    return JSONResponse({"status": "ok"})


app.include_router(github_router, prefix="/webhooks")
app.include_router(api_v1_router, prefix="/api/v1")

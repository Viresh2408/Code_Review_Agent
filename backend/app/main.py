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
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.security import verify_github_signature
from app.tasks import process_pr_review

# ── Structlog configuration ───────────────────────────────────────────────────
# Outputs JSON in production, pretty-printed in development (NFR-5)
settings = get_settings()

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer()
        if settings.app_env == "production"
        else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

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

# ── Events ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    logger.info("backend_starting", env=settings.app_env, log_level=settings.log_level)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("backend_shutting_down")


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    """Liveness probe — used by Docker healthcheck and load balancers."""
    return JSONResponse({"status": "ok"})


# Events we care about — all others are acknowledged but ignored.
_HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


@app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED, tags=["webhooks"])
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default="", alias="X-GitHub-Event"),
    x_github_delivery: str = Header(default="", alias="X-GitHub-Delivery"),
) -> JSONResponse:
    """
    GitHub App webhook endpoint.

    Processing contract (FR-1 of BRD):
    1. Verify HMAC-SHA256 signature → 401 on failure
    2. Parse payload
    3. Ignore non-PR events quickly (respond < 500ms)
    4. For eligible PR events: enqueue Celery task, respond 202 immediately
    5. Idempotency: (repo, pr_number, commit_sha) dedup happens in the task
    """
    log = logger.bind(
        delivery_id=x_github_delivery,
        github_event=x_github_event,
    )

    # ── Step 1: Verify signature ──────────────────────────────────────────────
    raw_body = await verify_github_signature(request, settings.github_webhook_secret)

    # ── Step 2: Parse payload ─────────────────────────────────────────────────
    try:
        payload: dict = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        log.warning("webhook_invalid_json", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    # ── Step 3: Route by event type ───────────────────────────────────────────
    if x_github_event != "pull_request":
        log.debug("webhook_event_ignored", reason="not a pull_request event")
        return JSONResponse({"received": True, "processed": False})

    action: str = payload.get("action", "")
    if action not in _HANDLED_ACTIONS:
        log.debug("webhook_action_ignored", action=action)
        return JSONResponse({"received": True, "processed": False})

    # ── Step 4: Extract PR metadata ───────────────────────────────────────────
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {}).get("full_name", "")
    pr_number: int = pr.get("number", 0)
    commit_sha: str = pr.get("head", {}).get("sha", "")
    installation_id: int = payload.get("installation", {}).get("id", 0)

    if not all([repo, pr_number, commit_sha]):
        log.warning("webhook_missing_fields", repo=repo, pr_number=pr_number, commit_sha=commit_sha)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload missing required fields: repository, pr_number, or commit_sha.",
        )

    log = log.bind(repo=repo, pr_number=pr_number, commit_sha=commit_sha, action=action)

    # ── Phase 0 Milestone: Log the full payload ───────────────────────────────
    log.info(
        "webhook_received",
        pr_title=pr.get("title", ""),
        pr_author=pr.get("user", {}).get("login", ""),
        installation_id=installation_id,
        # Log truncated payload summary (not the full diff — NFR-4)
        changed_files=pr.get("changed_files", "unknown"),
        additions=pr.get("additions", "unknown"),
        deletions=pr.get("deletions", "unknown"),
    )

    # ── Step 5: Enqueue Celery task ───────────────────────────────────────────
    task = process_pr_review.apply_async(
        kwargs={
            "repo": repo,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "installation_id": installation_id,
            "action": action,
        },
        # Natural idempotency key — Celery won't block a duplicate, but the
        # task handler will dedup against the DB (implemented in Phase 1)
        task_id=f"pr-review-{repo.replace('/', '-')}-{pr_number}-{commit_sha[:8]}",
    )

    log.info("webhook_task_enqueued", task_id=task.id)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "received": True,
            "processed": True,
            "task_id": task.id,
            "repo": repo,
            "pr_number": pr_number,
        },
    )

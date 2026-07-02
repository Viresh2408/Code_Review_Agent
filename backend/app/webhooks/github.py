"""
GitHub Webhook receiver.
"""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.security import verify_github_signature
from app.tasks.review_job import process_pr_review

logger = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter()

# Actions on a PR that we want to trigger a review for
_HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


@router.post("/github", status_code=status.HTTP_200_OK)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default="", alias="X-GitHub-Event"),
    x_github_delivery: str = Header(default="", alias="X-GitHub-Delivery"),
) -> JSONResponse:
    """
    FastAPI endpoint for GitHub webhooks.

    Contract:
    1. Verify HMAC-SHA256 signature → 401 on failure (handled in verify_github_signature).
    2. Ignore non-pull_request events or unsupported actions → 200 OK (processed=False).
    3. Enqueue Celery task with (repo_full_name, pr_number, commit_sha) → 200 OK (processed=True).
    """
    log = logger.bind(
        delivery_id=x_github_delivery,
        github_event=x_github_event,
    )

    # ── Step 1: Verify Signature ──────────────────────────────────────────────
    raw_body = await verify_github_signature(request, settings.github_webhook_secret)

    # ── Step 2: Parse Payload ─────────────────────────────────────────────────
    try:
        payload: dict = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        log.warning("webhook_invalid_json", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    # ── Step 3: Event Filtering ───────────────────────────────────────────────
    if x_github_event != "pull_request":
        log.debug("webhook_event_ignored", reason=f"Ignored non-PR event: {x_github_event}")
        return JSONResponse({"received": True, "processed": False})

    action: str = payload.get("action", "")
    if action not in _HANDLED_ACTIONS:
        log.debug("webhook_action_ignored", action=action)
        return JSONResponse({"received": True, "processed": False})

    # ── Step 4: Extract PR Details ────────────────────────────────────────────
    pr = payload.get("pull_request", {})
    repo_full_name = payload.get("repository", {}).get("full_name", "")
    pr_number: int = pr.get("number", 0)
    commit_sha: str = pr.get("head", {}).get("sha", "")

    if not all([repo_full_name, pr_number, commit_sha]):
        log.warning(
            "webhook_missing_fields",
            repo=repo_full_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload is missing repository name, pr_number, or commit_sha.",
        )

    log = log.bind(repo=repo_full_name, pr_number=pr_number, commit_sha=commit_sha, action=action)
    log.info(
        "webhook_received",
        pr_title=pr.get("title", ""),
        pr_author=pr.get("user", {}).get("login", ""),
    )

    # ── Step 5: Enqueue Celery Task ───────────────────────────────────────────
    task_id = f"pr-review-{repo_full_name.replace('/', '-')}-{pr_number}-{commit_sha[:8]}"
    task = process_pr_review.apply_async(
        kwargs={
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
        },
        task_id=task_id,
    )

    log.info("webhook_task_enqueued", task_id=task.id)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "received": True,
            "processed": True,
            "task_id": task.id,
            "repo": repo_full_name,
            "pr_number": pr_number,
        },
    )

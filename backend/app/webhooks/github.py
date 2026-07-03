"""
GitHub Webhook receiver.
"""

from __future__ import annotations

import json
import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import get_settings
from app.security import verify_github_signature
from app.tasks.review_job import process_pr_review, index_repo_conventions_task
from app.db.session import get_session
from app.models import Installation, Repo

logger = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter()

# Actions on a PR that we want to trigger a review for
_HANDLED_PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})
_SUPPORTED_EVENTS = {"pull_request", "installation", "installation_repositories"}


# ── Webhook Payload Handlers ──────────────────────────────────────────────────

async def handle_installation_created(payload: dict) -> None:
    """Handle installation.created to register the app installation and monitor repos."""
    github_installation_id = payload.get("installation", {}).get("id")
    account_login = payload.get("installation", {}).get("account", {}).get("login")
    repositories = payload.get("repositories", [])
    
    async with get_session() as session:
        # Create or update installation record
        stmt = select(Installation).where(Installation.github_installation_id == github_installation_id)
        res = await session.execute(stmt)
        installation = res.scalar_one_or_none()
        if not installation:
            installation = Installation(
                github_installation_id=github_installation_id,
                account_login=account_login,
            )
            session.add(installation)
        else:
            installation.account_login = account_login
            
        for repo_info in repositories:
            repo_id = repo_info.get("id")
            repo_name = repo_info.get("name")
            repo_full_name = repo_info.get("full_name")
            owner = repo_full_name.split("/")[0] if "/" in repo_full_name else account_login
            
            repo_stmt = select(Repo).where(Repo.github_repo_id == repo_id)
            repo_res = await session.execute(repo_stmt)
            repo_obj = repo_res.scalar_one_or_none()
            if not repo_obj:
                repo_obj = Repo(
                    github_repo_id=repo_id,
                    owner=owner,
                    name=repo_name,
                    is_active=True,
                )
                session.add(repo_obj)
            else:
                repo_obj.is_active = True
                
            # Trigger conventions indexing in the background via Celery
            index_repo_conventions_task.delay(
                repo_full_name=repo_full_name,
                installation_id=github_installation_id
            )


async def handle_installation_repositories(payload: dict) -> None:
    """Handle installation_repositories events to add or remove repos dynamically."""
    action = payload.get("action")
    github_installation_id = payload.get("installation", {}).get("id")
    account_login = payload.get("installation", {}).get("account", {}).get("login")
    
    async with get_session() as session:
        if action == "added":
            repos_added = payload.get("repositories_added", [])
            for repo_info in repos_added:
                repo_id = repo_info.get("id")
                repo_name = repo_info.get("name")
                repo_full_name = repo_info.get("full_name")
                owner = repo_full_name.split("/")[0] if "/" in repo_full_name else account_login
                
                repo_stmt = select(Repo).where(Repo.github_repo_id == repo_id)
                repo_res = await session.execute(repo_stmt)
                repo_obj = repo_res.scalar_one_or_none()
                if not repo_obj:
                    repo_obj = Repo(
                        github_repo_id=repo_id,
                        owner=owner,
                        name=repo_name,
                        is_active=True,
                    )
                    session.add(repo_obj)
                else:
                    repo_obj.is_active = True
                    
                # Trigger conventions indexing
                index_repo_conventions_task.delay(
                    repo_full_name=repo_full_name,
                    installation_id=github_installation_id
                )
        elif action == "removed":
            repos_removed = payload.get("repositories_removed", [])
            for repo_info in repos_removed:
                repo_id = repo_info.get("id")
                repo_stmt = select(Repo).where(Repo.github_repo_id == repo_id)
                repo_res = await session.execute(repo_stmt)
                repo_obj = repo_res.scalar_one_or_none()
                if repo_obj:
                    repo_obj.is_active = False


# ── Webhook Endpoint ─────────────────────────────────────────────────────────

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
    2. Support PR events, App installation events, and dynamic repo scope changes.
    3. Enqueue review tasks for PRs and indexing tasks for new repos.
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
    if x_github_event not in _SUPPORTED_EVENTS:
        log.debug("webhook_event_ignored", reason=f"Ignored event: {x_github_event}")
        return JSONResponse({"received": True, "processed": False})

    action: str = payload.get("action", "")

    # ── Step 4: Handle Installation Events (Dynamic Setup) ────────────────────
    if x_github_event == "installation":
        if action == "created":
            log.info("webhook_installation_created", account=payload.get("installation", {}).get("account", {}).get("login"))
            await handle_installation_created(payload)
            return JSONResponse({"received": True, "processed": True, "event": "installation_created"})
        else:
            log.debug("webhook_installation_action_ignored", action=action)
            return JSONResponse({"received": True, "processed": False})

    if x_github_event == "installation_repositories":
        log.info("webhook_installation_repositories_changed", action=action)
        await handle_installation_repositories(payload)
        return JSONResponse({"received": True, "processed": True, "event": f"repositories_{action}"})

    # ── Step 5: Handle Pull Request Review Core Loop ──────────────────────────
    if x_github_event == "pull_request":
        if action not in _HANDLED_PR_ACTIONS:
            log.debug("webhook_action_ignored", action=action)
            return JSONResponse({"received": True, "processed": False})

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

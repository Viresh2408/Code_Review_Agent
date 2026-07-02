"""
Celery review job task stub.
"""

from __future__ import annotations

import asyncio
import structlog
from sqlalchemy import select

from app.db.session import get_session
from app.models import PullRequest, Repo
from app.tasks.celery_app import celery_app

logger = structlog.get_logger(__name__)


async def check_idempotency(repo_full_name: str, pr_number: int, commit_sha: str) -> bool:
    """
    Check if this PR commit has already been reviewed or is currently being reviewed.
    Returns True if it is a duplicate and should be skipped.
    """
    if "/" not in repo_full_name:
        return False

    owner, name = repo_full_name.split("/", 1)

    async with get_session() as session:
        # Find the repository ID first
        repo_stmt = select(Repo).where(Repo.owner == owner, Repo.name == name)
        repo_res = await session.execute(repo_stmt)
        repo = repo_res.scalar_one_or_none()
        if not repo:
            return False

        # Check for pull request record with matching identity and finished/running review
        pr_stmt = select(PullRequest).where(
            PullRequest.repo_id == repo.id,
            PullRequest.pr_number == pr_number,
            PullRequest.commit_sha == commit_sha,
        )
        pr_res = await session.execute(pr_stmt)
        pr = pr_res.scalar_one_or_none()
        if not pr:
            return False

        # If already completed or actively processing, skip the new review task
        if pr.status in ("completed", "processing"):
            return True

        return False


@celery_app.task(
    name="tasks.process_pr_review",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
)
def process_pr_review(
    self,
    *,
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    **kwargs,
) -> dict:
    """
    Process a PR review run. Checks idempotency using DB before executing logic.
    """
    log = logger.bind(
        task_id=self.request.id,
        repo=repo_full_name,
        pr_number=pr_number,
        commit_sha=commit_sha,
    )
    log.info("pr_review_task_started")

    try:
        is_dup = asyncio.run(check_idempotency(repo_full_name, pr_number, commit_sha))
    except Exception as exc:
        log.error("idempotency_check_failed", error=str(exc))
        is_dup = False

    if is_dup:
        log.info(
            "pr_review_task_skipped_duplicate",
            reason="Review for this commit SHA is already completed or processing.",
        )
        return {
            "status": "skipped_duplicate",
            "repo": repo_full_name,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
        }

    try:
        log.info("pr_review_task_ingestion_started")
        from app.parser.pipeline import ingest_pr
        pr_context = ingest_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
        )
        log.info(
            "pr_review_task_ingestion_completed",
            changed_files_count=len(pr_context.changed_files),
        )
        return {
            "status": "ingested",
            "repo": repo_full_name,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "changed_files_count": len(pr_context.changed_files),
        }
    except Exception as exc:
        log.error("pr_review_task_ingestion_failed", error=str(exc))
        raise

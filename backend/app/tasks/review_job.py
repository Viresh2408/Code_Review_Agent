"""
Celery review job task stub.
"""

from __future__ import annotations

import asyncio
import structlog
from sqlalchemy import select, func

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


async def update_pr_status(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    status: str,
) -> None:
    """
    Create or update the PullRequest status in the database.
    """
    if "/" not in repo_full_name:
        return

    owner, name = repo_full_name.split("/", 1)

    async with get_session() as session:
        # Find the repository first
        repo_stmt = select(Repo).where(Repo.owner == owner, Repo.name == name)
        repo_res = await session.execute(repo_stmt)
        repo = repo_res.scalar_one_or_none()
        if not repo:
            # If repo doesn't exist, we don't have a record to attach the PR to.
            return

        # Check if the PullRequest record exists
        pr_stmt = select(PullRequest).where(
            PullRequest.repo_id == repo.id,
            PullRequest.pr_number == pr_number,
            PullRequest.commit_sha == commit_sha,
        )
        pr_res = await session.execute(pr_stmt)
        pr = pr_res.scalar_one_or_none()

        if pr:
            pr.status = status
            if status == "completed":
                pr.completed_at = func.now()
        else:
            pr = PullRequest(
                repo_id=repo.id,
                pr_number=pr_number,
                commit_sha=commit_sha,
                status=status,
            )
            if status == "completed":
                pr.completed_at = func.now()
            session.add(pr)

        await session.commit()


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

    # Mark PR as processing in the DB
    try:
        asyncio.run(update_pr_status(repo_full_name, pr_number, commit_sha, "processing"))
    except Exception as exc:
        log.warning("failed_to_update_pr_status_to_processing", error=str(exc))

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

        # Run review pipeline
        log.info("pr_review_task_pipeline_started")
        from agents.orchestrator import graph, post_findings_to_github
        result_state = graph.invoke(pr_context)

        findings = result_state.findings if hasattr(result_state, "findings") else result_state.get("findings", [])
        log.info("pr_review_task_pipeline_completed", findings_count=len(findings))

        # Post back to GitHub
        log.info("pr_review_task_posting_started")
        post_findings_to_github(pr_context, findings)
        log.info("pr_review_task_posting_completed")

        # Mark PR as completed in the DB
        try:
            asyncio.run(update_pr_status(repo_full_name, pr_number, commit_sha, "completed"))
        except Exception as exc:
            log.warning("failed_to_update_pr_status_to_completed", error=str(exc))

        return {
            "status": "completed",
            "repo": repo_full_name,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "changed_files_count": len(pr_context.changed_files),
            "findings_count": len(findings),
        }
    except Exception as exc:
        log.error("pr_review_task_failed", error=str(exc))
        # Mark PR as failed in the DB
        try:
            asyncio.run(update_pr_status(repo_full_name, pr_number, commit_sha, "failed"))
        except Exception as db_exc:
            log.warning("failed_to_update_pr_status_to_failed", error=str(db_exc))
        raise

"""
Celery task definitions.

Phase 0: Tasks are stubs — they log the job and return immediately.
Subsequent phases will fill in the actual agent orchestration.
"""

from __future__ import annotations

import structlog

from app.worker import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(
    name="tasks.process_pr_review",
    bind=True,
    max_retries=3,
    default_retry_delay=30,  # seconds; Celery doubles this on each retry (exponential)
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
)
def process_pr_review(
    self,
    *,
    repo: str,
    pr_number: int,
    commit_sha: str,
    installation_id: int,
    action: str,
) -> dict:
    """
    Main PR review task enqueued by the webhook handler.

    Phase 0: logs the job and returns.
    Phase 1+: will call the ingestion pipeline → LangGraph orchestrator.

    Args:
        repo:            Full repo name, e.g. "owner/repo-name"
        pr_number:       GitHub PR number
        commit_sha:      Head commit SHA (used for idempotency)
        installation_id: GitHub App installation ID (for token generation)
        action:          Webhook action: "opened" | "synchronize" | "reopened"
    """
    log = logger.bind(
        task_id=self.request.id,
        repo=repo,
        pr_number=pr_number,
        commit_sha=commit_sha,
        installation_id=installation_id,
        action=action,
    )

    log.info("pr_review_task_started")

    # ── Phase 0 stub ──────────────────────────────────────────────────────────
    # TODO(Phase 1): Replace with actual ingestion + agent orchestration
    log.info(
        "pr_review_task_stub",
        message="Phase 0 stub: no analysis yet. This task will be replaced in Phase 1.",
    )

    return {
        "status": "stub_completed",
        "repo": repo,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
    }

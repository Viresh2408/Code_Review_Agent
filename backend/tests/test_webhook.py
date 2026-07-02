"""
Smoke tests and task unit tests for the FastAPI webhook and Celery task.

Tests:
  1. GET /health → 200 {"status": "ok"}
  2. POST /webhooks/github with a missing signature → 401
  3. POST /webhooks/github with an invalid signature → 401
  4. POST /webhooks/github with a non-PR event → 200 (processed=False)
  5. POST /webhooks/github with a valid PR event → 200 (processed=True)
  6. Task check_idempotency → mock DB to verify task completes or skips correctly
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

# Patch environment variables before importing anything
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/codereview")
os.environ.setdefault("SYNC_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/codereview")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tasks.review_job import process_pr_review

WEBHOOK_SECRET = "test-secret"


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def make_signature(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    sig = hmac.new(
        key=secret.encode(),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


SAMPLE_PAYLOAD = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "head": {"sha": "abc1234567890"},
        "title": "Add feature X",
        "user": {"login": "testuser"},
        "changed_files": 3,
        "additions": 100,
        "deletions": 20,
    },
    "repository": {"full_name": "testowner/testrepo", "id": 999},
    "installation": {"id": 777},
}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_webhook_missing_signature(client):
    resp = client.post("/webhooks/github", json=SAMPLE_PAYLOAD)
    assert resp.status_code == 401
    assert "Missing X-Hub-Signature-256 header" in resp.text


def test_webhook_invalid_signature(client):
    body = json.dumps(SAMPLE_PAYLOAD).encode()
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "test-delivery-001",
            "X-Hub-Signature-256": "sha256=badhash",
        },
    )
    assert resp.status_code == 401
    assert "Webhook signature verification failed" in resp.text


def test_webhook_non_pr_event(client):
    body = json.dumps({"action": "created"}).encode()
    sig = make_signature(body)
    resp = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "test-delivery-002",
            "X-Hub-Signature-256": sig,
        },
    )
    # Should acknowledge but not process
    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["processed"] is False


def test_webhook_valid_pr_opened(client):
    """Valid PR webhook — should enqueue and return 200."""
    mock_task = MagicMock()
    mock_task.id = "test-task-id-001"

    body = json.dumps(SAMPLE_PAYLOAD).encode()
    sig = make_signature(body)

    with patch("app.webhooks.github.process_pr_review") as mock_pr_task:
        mock_pr_task.apply_async.return_value = mock_task
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "test-delivery-003",
                "X-Hub-Signature-256": sig,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["processed"] is True
    assert data["repo"] == "testowner/testrepo"
    assert data["pr_number"] == 42
    assert data["task_id"] == "test-task-id-001"


# ── Task Idempotency Unit Tests ───────────────────────────────────────────────

def test_task_idempotency_not_duplicate():
    """If PR does not exist in the DB, the task should run fully and complete."""
    # Mock database session to return None for both queries (Repo and PR)
    mock_session = AsyncMock()
    mock_session.execute.return_value.scalar_one_or_none.return_value = None

    with patch("app.tasks.review_job.get_session") as mock_get_session, \
         patch("app.parser.pipeline.ingest_pr") as mock_ingest_pr:
        # get_session is an async context manager
        mock_get_session.return_value.__aenter__.return_value = mock_session

        # Mock ingest_pr to return a dummy context with 1 file
        mock_context = MagicMock()
        mock_context.changed_files = [MagicMock()]
        mock_ingest_pr.return_value = mock_context

        # Push mock context to Celery's request stack to simulate running task context
        from celery.app.task import Context
        process_pr_review.request_stack.push(Context(id="test-celery-run-id"))
        try:
            result = process_pr_review._orig_run(
                repo_full_name="owner/repo",
                pr_number=1,
                commit_sha="sha123456",
            )
        finally:
            process_pr_review.request_stack.pop()

        assert result["status"] == "ingested"
        assert result["repo"] == "owner/repo"
        assert result["pr_number"] == 1
        assert result["changed_files_count"] == 1


def test_task_idempotency_is_duplicate():
    """If PR exists and status is completed, the task should skip gracefully."""
    mock_session = AsyncMock()

    # Mock DB objects
    mock_repo = MagicMock()
    mock_repo.id = 100

    mock_pr = MagicMock()
    mock_pr.status = "completed"

    # Set sequential return values for session.execute() calls:
    # 1st call for Repo, 2nd call for PullRequest
    repo_result = MagicMock()
    repo_result.scalar_one_or_none.return_value = mock_repo

    pr_result = MagicMock()
    pr_result.scalar_one_or_none.return_value = mock_pr

    mock_session.execute.side_effect = [repo_result, pr_result]

    with patch("app.tasks.review_job.get_session") as mock_get_session:
        mock_get_session.return_value.__aenter__.return_value = mock_session

        # Push mock context to Celery's request stack to simulate running task context
        from celery.app.task import Context
        process_pr_review.request_stack.push(Context(id="test-celery-run-id"))
        try:
            result = process_pr_review._orig_run(
                repo_full_name="owner/repo",
                pr_number=1,
                commit_sha="sha123456",
            )
        finally:
            process_pr_review.request_stack.pop()

        assert result["status"] == "skipped_duplicate"
        assert result["repo"] == "owner/repo"
        assert result["pr_number"] == 1

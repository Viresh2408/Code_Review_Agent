"""
Smoke test for the FastAPI webhook endpoint.

Tests:
  1. GET /health → 200 {"status": "ok"}
  2. POST /webhooks/github with a missing signature → 401
  3. POST /webhooks/github with a valid HMAC signature + pull_request payload → 202
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

# Patch settings before importing the app
import os
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/codereview")
os.environ.setdefault("SYNC_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/codereview")


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


WEBHOOK_SECRET = "test-secret"


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
    assert resp.json()["processed"] is False


def test_webhook_valid_pr_opened(client, monkeypatch):
    """Valid PR webhook — should enqueue and return 202."""
    # Monkeypatch Celery task to avoid needing a real Redis
    from unittest.mock import MagicMock, patch

    mock_task = MagicMock()
    mock_task.id = "test-task-id-001"

    body = json.dumps(SAMPLE_PAYLOAD).encode()
    sig = make_signature(body)

    with patch("app.main.process_pr_review") as mock_pr_task:
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

    assert resp.status_code == 202
    data = resp.json()
    assert data["received"] is True
    assert data["processed"] is True
    assert data["repo"] == "testowner/testrepo"
    assert data["pr_number"] == 42

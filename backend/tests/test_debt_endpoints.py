from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

# Set required environment variables before imports
os.environ.setdefault("GITHUB_APP_ID", "12345")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("APP_ENV", "development")

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.main import app
from app.db.crud import record_debt_score, get_last_debt_score, save_review_and_findings
from app.models import DebtScore, Finding, Review


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── CRUD Logic Unit Tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_debt_score():
    """Verify record_debt_score correctly creates and flushes a DebtScore model."""
    mock_session = AsyncMock()
    
    score_obj = await record_debt_score(
        repo_id=1,
        file_path="src/orders.py",
        score=12.5,
        delta=1.5,
        pr_number=42,
        session=mock_session,
    )
    
    assert score_obj.repo_id == 1
    assert score_obj.file_path == "src/orders.py"
    assert score_obj.score == 12.5
    assert score_obj.delta == 1.5
    assert score_obj.pr_number == 42
    mock_session.add.assert_called_once_with(score_obj)
    mock_session.flush.assert_called_once()


@pytest.mark.asyncio
async def test_get_last_debt_score_empty():
    """Verify get_last_debt_score returns 0.0 when no records exist."""
    mock_session = AsyncMock()
    
    # Mock database to return empty list / None
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result
    
    last_score = await get_last_debt_score(repo_id=1, file_path="src/orders.py", session=mock_session)
    assert last_score == 0.0


@pytest.mark.asyncio
async def test_get_last_debt_score_existing():
    """Verify get_last_debt_score returns the last score value when it exists."""
    mock_session = AsyncMock()
    
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = 15.5
    mock_session.execute.return_value = mock_result
    
    last_score = await get_last_debt_score(repo_id=1, file_path="src/orders.py", session=mock_session)
    assert last_score == 15.5


@pytest.mark.asyncio
async def test_save_review_and_findings_first_time():
    """Verify save_review_and_findings handles first-time scores and inserts models."""
    mock_session = AsyncMock()
    
    # Mock Repo query
    mock_repo = MagicMock()
    mock_repo.id = 1
    
    # Mock PR query (None -> create it)
    # Mock last score query (None -> 0.0)
    mock_execute_results = []
    
    # Repo check result
    mock_repo_res = MagicMock()
    mock_repo_res.scalar_one_or_none.return_value = mock_repo
    mock_execute_results.append(mock_repo_res)
    
    # PR check result
    mock_pr_res = MagicMock()
    mock_pr_res.scalar_one_or_none.return_value = None
    mock_execute_results.append(mock_pr_res)
    
    # Last score result (for the file changed)
    mock_score_res = MagicMock()
    mock_score_res.scalar_one_or_none.return_value = None
    mock_execute_results.append(mock_score_res)
    
    mock_session.execute.side_effect = mock_execute_results
    
    findings = [
        {
            "agent": "security_agent",
            "file_path": "src/orders.py",
            "line": 10,
            "severity": "blocker",
            "category": "security",
            "message": "SQL Injection vulnerability.",
            "confidence": 0.9,
            "suggested_fix": None,
        }
    ]
    
    # ChangedFileSchema mock
    mock_file = MagicMock()
    mock_file.path = "src/orders.py"
    mock_file.diff_hunks = ["@@ -1,4 +1,5 @@\n+print(x)"]
    mock_file.language = "python"
    
    with patch("app.db.crud.get_complexity_delta", return_value=1):
        review = await save_review_and_findings(
            repo_owner="owner",
            repo_name="repo",
            pr_number=42,
            commit_sha="commit123",
            findings=findings,
            changed_files=[mock_file],
            session=mock_session,
        )
        
        assert review.total_findings == 1
        assert review.blocker_count == 1
        assert review.warning_count == 0
        assert mock_session.commit.called


# ── REST API Router Integration Tests ─────────────────────────────────────────

def test_api_endpoints_auth_required(client):
    """Verify API endpoints return 401 when Authorization header is missing."""
    # GET /api/v1/repos/1/debt-trend
    res = client.get("/api/v1/repos/1/debt-trend")
    assert res.status_code == status.HTTP_401_UNAUTHORIZED
    
    # GET /api/v1/reviews/1/findings
    res = client.get("/api/v1/reviews/1/findings")
    assert res.status_code == status.HTTP_401_UNAUTHORIZED


def test_api_endpoints_auth_invalid(client):
    """Verify API endpoints return 401 on invalid JWT tokens."""
    headers = {"Authorization": "Bearer invalid-token"}
    
    res = client.get("/api/v1/repos/1/debt-trend", headers=headers)
    assert res.status_code == status.HTTP_401_UNAUTHORIZED
    
    res = client.get("/api/v1/reviews/1/findings", headers=headers)
    assert res.status_code == status.HTTP_401_UNAUTHORIZED


def test_api_endpoints_dev_token_success(client):
    """Verify Bearer dev-token bypass works in development environment."""
    headers = {"Authorization": "Bearer dev-token"}
    
    mock_session = AsyncMock()
    
    # Mock DebtScore query results
    mock_result = MagicMock()
    mock_row1 = MagicMock()
    mock_row1.day = "2026-07-01"
    mock_row1.file_path = "src/orders.py"
    mock_row1.end_of_day_score = 12.5
    mock_result.all.return_value = [mock_row1]
    
    mock_session.execute.return_value = mock_result
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    
    with patch("app.api.v1.endpoints.get_session", return_value=mock_ctx):
        # Test trend endpoint
        res = client.get("/api/v1/repos/1/debt-trend", headers=headers)
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert "trend" in data
        assert len(data["trend"]) == 1
        assert data["trend"][0]["file_path"] == "src/orders.py"
        assert data["trend"][0]["score"] == 12.5


def test_api_endpoints_debt_trend_empty(client):
    """Verify trend endpoint returns empty list when no data is in DB."""
    headers = {"Authorization": "Bearer dev-token"}
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    
    mock_session.execute.return_value = mock_result
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    
    with patch("app.api.v1.endpoints.get_session", return_value=mock_ctx):
        res = client.get("/api/v1/repos/1/debt-trend", headers=headers)
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert data == {"trend": []}


def test_api_endpoints_findings_success(client):
    """Verify findings endpoint returns list of findings with proper attributes."""
    headers = {"Authorization": "Bearer dev-token"}
    mock_session = AsyncMock()
    
    # Mock findings list
    finding = Finding(
        id=501,
        review_id=1,
        agent="security_agent",
        file_path="src/orders.py",
        line_number=10,
        severity="blocker",
        category="security",
        message="SQL injection.",
        confidence=0.95,
        escalated_to_claude=False,
        suggested_fix="Use ORM",
    )
    
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [finding]
    
    mock_session.execute.return_value = mock_result
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    
    with patch("app.api.v1.endpoints.get_session", return_value=mock_ctx):
        res = client.get("/api/v1/reviews/1/findings?limit=10&offset=0", headers=headers)
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert "findings" in data
        assert len(data["findings"]) == 1
        assert data["findings"][0]["id"] == 501
        assert data["findings"][0]["file_path"] == "src/orders.py"
        assert data["findings"][0]["severity"] == "blocker"


def test_api_endpoints_repos_success(client):
    """Verify repos endpoint returns list of active repositories with stats."""
    from app.models import Repo
    headers = {"Authorization": "Bearer dev-token"}
    mock_session = AsyncMock()

    # Mock Repo object
    repo = Repo(
        id=1,
        github_repo_id=12345678,
        owner="owner",
        name="name",
    )

    # We need to mock the executions inside the loop:
    # 1. select(Repo) -> returns [repo]
    # 2. distinct file_path select -> returns []
    # 3. review_stmt select -> returns stats
    mock_res_repo = MagicMock()
    mock_res_repo.scalars.return_value.all.return_value = [repo]

    mock_res_files = MagicMock()
    mock_res_files.scalars.return_value.all.return_value = []

    mock_res_stats = MagicMock()
    mock_res_stats.first.return_value = MagicMock(review_count=3, total_findings=5)

    mock_session.execute.side_effect = [mock_res_repo, mock_res_files, mock_res_stats]
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session

    with patch("app.api.v1.endpoints.get_session", return_value=mock_ctx):
        res = client.get("/api/v1/repos", headers=headers)
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert "repos" in data
        assert len(data["repos"]) == 1
        assert data["repos"][0]["id"] == 1
        assert data["repos"][0]["full_name"] == "owner/name"
        assert data["repos"][0]["review_count"] == 3
        assert data["repos"][0]["total_findings"] == 5


def test_api_endpoints_repo_reviews_success(client):
    """Verify repo reviews endpoint returns list of reviews."""
    from app.models import Review, PullRequest
    headers = {"Authorization": "Bearer dev-token"}
    mock_session = AsyncMock()

    # Mock Review and related PR
    pr = PullRequest(id=10, pr_number=12, commit_sha="abcdef", title="Fix bug")
    review = Review(
        id=201,
        pull_request_id=10,
        total_findings=2,
        blocker_count=0,
        warning_count=1,
        nit_count=1,
    )
    # Set relationship manually
    review.pull_request = pr

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [review]

    mock_session.execute.return_value = mock_result
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session

    with patch("app.api.v1.endpoints.get_session", return_value=mock_ctx):
        res = client.get("/api/v1/repos/1/reviews?limit=10&offset=0", headers=headers)
        assert res.status_code == status.HTTP_200_OK
        data = res.json()
        assert "reviews" in data
        assert len(data["reviews"]) == 1
        assert data["reviews"][0]["id"] == 201
        assert data["reviews"][0]["pr_number"] == 12
        assert data["reviews"][0]["commit_sha"] == "abcdef"
        assert data["reviews"][0]["total_findings"] == 2


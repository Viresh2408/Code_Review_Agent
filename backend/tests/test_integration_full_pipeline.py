import asyncio
import json
import os
import time
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import select
import docker
try:
    docker.from_env().ping()
    docker_available = True
except Exception:
    docker_available = False

if docker_available:
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer
else:
    # Dummy containers for import safety when skipping
    PostgresContainer = None
    RedisContainer = None

import celery.exceptions

# Ephemeral PostgreSQL and Redis containers
@pytest.fixture(scope="session")
def postgres_container():
    if not docker_available:
        pytest.skip("Docker daemon is not running. Skipped testcontainers integration test.")
    with PostgresContainer("postgres:16-alpine") as postgres:
        yield postgres

@pytest.fixture(scope="session")
def redis_container():
    if not docker_available:
        pytest.skip("Docker daemon is not running. Skipped testcontainers integration test.")
    with RedisContainer("redis:7-alpine") as redis_srv:
        yield redis_srv


@pytest.fixture(scope="session")
def setup_databases(postgres_container, redis_container):
    # Patch Database and Redis URLs
    db_url = postgres_container.get_connection_url(driver="asyncpg")
    sync_db_url = postgres_container.get_connection_url(driver="psycopg2")
    redis_url = f"redis://{redis_container.get_container_host_ip()}:{redis_container.get_exposed_port(6379)}/0"
    
    import app.config
    import app.db.session
    from app.config import Settings
    
    # Override settings
    settings = app.config.get_settings()
    settings.database_url = db_url
    settings.sync_database_url = sync_db_url
    settings.celery_broker_url = redis_url
    settings.celery_result_backend = redis_url
    
    # Recreate the async engine & SessionLocal on the patched URL
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    test_engine = create_async_engine(db_url, echo=True)
    test_session_local = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    
    # Patch session variables
    app.db.session.async_engine = test_engine
    app.db.session.AsyncSessionLocal = test_session_local
    
    # Initialize schema
    from app.db.base import Base
    async def init_schema():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    asyncio.run(init_schema())
    
    yield test_engine

@pytest.mark.asyncio
@patch("agents.orchestrator.Groq")
@patch("agents.orchestrator.Anthropic")
@patch("app.parser.pipeline.Github")
@patch("agents.orchestrator.Github")
async def test_integration_full_pipeline_with_celery_retry(
    mock_orch_github_class,
    mock_pipeline_github_class,
    mock_anthropic_class,
    mock_groq_class,
    setup_databases
):
    """
    Full pipeline integration test verifying:
    1. Running full review task process_pr_review end-to-end.
    2. Verification of the Celery retry mechanism when save fails on the first attempt.
    """
    engine = setup_databases
    
    # Mock LLM API clients
    mock_groq_client = MagicMock()
    mock_groq_class.return_value = mock_groq_client
    
    mock_anthropic_client = MagicMock()
    mock_anthropic_class.return_value = mock_anthropic_client
    
    # Set mock response for Groq (primary review model)
    mock_groq_client.chat.completions.create.return_value = MagicMock(
        usage=MagicMock(prompt_tokens=100, completion_tokens=50),
        choices=[MagicMock(message=MagicMock(content=json.dumps({
            "findings": [
                {
                    "line": 10,
                    "severity": "warning",
                    "message": "Integration test finding.",
                    "confidence": 0.85,
                    "suggested_fix": None
                }
            ]
        })))]
    )
    
    # Set mock response for Anthropic (escalation / debt scoring fallback model)
    mock_haiku_response = MagicMock()
    mock_haiku_response.usage.input_tokens = 50
    mock_haiku_response.usage.output_tokens = 20
    mock_haiku_response.content = [
        MagicMock(text=json.dumps({
            "multiplier": 1.0,
            "reason": "Test debt reason"
        }))
    ]
    mock_anthropic_client.messages.create.return_value = mock_haiku_response
    
    # Mock Github API components
    mock_github = MagicMock()
    mock_pipeline_github_class.return_value = mock_github
    mock_orch_github_class.return_value = mock_github
    
    mock_repo = MagicMock()
    mock_github.get_repo.return_value = mock_repo
    
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    mock_pr.head.sha = "testsha123"
    
    # Mock file changes returned by Github
    mock_file1 = MagicMock()
    mock_file1.filename = "main.py"
    mock_file1.patch = "@@ -1,3 +1,6 @@\n def hello():\n+    print('hello world')\n+    return True"
    mock_pr.get_files.return_value = [mock_file1]

    # Pre-seed the Repo in Postgres
    from app.db.session import get_session
    from app.models import Repo
    async with get_session() as session:
        # Check if repo exists
        stmt = select(Repo).where(Repo.owner == "testowner", Repo.name == "testrepo")
        res = await session.execute(stmt)
        repo_obj = res.scalar_one_or_none()
        if not repo_obj:
            repo_obj = Repo(owner="testowner", name="testrepo")
            session.add(repo_obj)
            await session.commit()
            
    # Mock the database save to raise on first attempt, then succeed on second attempt
    import app.db.crud
    from app.tasks.review_job import process_pr_review
    
    original_save = app.db.crud.save_review_and_findings
    save_calls = []

    async def mock_save(*args, **kwargs):
        save_calls.append(1)
        if len(save_calls) == 1:
            raise Exception("Mock DB Save Error - forcing retry")
        return await original_save(*args, **kwargs)

    # Enable eager mode on celery_app
    from app.tasks.celery_app import celery_app
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    
    with patch("app.db.crud.save_review_and_findings", side_effect=mock_save):
        # We patch the process_pr_review task's retry method to track calls
        with patch.object(process_pr_review, "retry", side_effect=process_pr_review.retry) as mock_retry:
            try:
                # Run task synchronously in eager mode
                res = process_pr_review.apply_async(
                    kwargs={
                        "repo_full_name": "testowner/testrepo",
                        "pr_number": 1,
                        "commit_sha": "testsha123"
                    }
                )
            except Exception as e:
                # Eager runner propagates Retry exception or the DB exception
                pass
                
            # Verify retry was attempted
            assert mock_retry.called
            # Verify retry config: max_retries is 3
            assert process_pr_review.max_retries == 3

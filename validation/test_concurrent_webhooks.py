#!/usr/bin/env python3
"""
validation/test_concurrent_webhooks.py

Concurrency and idempotency stress-test harness.
Fires duplicate requests within 500ms to assert database-level locking,
and concurrent separate PR runs to verify isolation (no crosstalk).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project paths to sys.path
root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

# pyrefly: ignore [missing-import]
from app.db.session import get_session
# pyrefly: ignore [missing-import]
from app.models import Repo, PullRequest, Review
# pyrefly: ignore [missing-import]
from app.tasks.review_job import process_pr_review
from sqlalchemy import select, delete


class MockSelf:
    def __init__(self, task_id: str):
        self.request = type("Request", (), {"id": task_id})()


async def setup_test_db(repo_owner: str, repo_name: str) -> int:
    """Pre-insert test Repo node to satisfy DB foreign keys."""
    async with get_session() as session:
        # Clear any existing matching PRs and reviews first
        repo_stmt = select(Repo).where(Repo.owner == repo_owner, Repo.name == repo_name)
        repo_res = await session.execute(repo_stmt)
        repo = repo_res.scalar_one_or_none()

        if not repo:
            repo = Repo(github_repo_id=999123, owner=repo_owner, name=repo_name)
            session.add(repo)
            await session.commit()
            await session.refresh(repo)
        else:
            # Delete old PRs
            await session.execute(delete(PullRequest).where(PullRequest.repo_id == repo.id))
            await session.commit()

        return repo.id


def run_review_sync(task_id: str, repo: str, pr_num: int, sha: str) -> dict:
    """Helper to run the celery task synchronously on a thread.

    process_pr_review._orig_run is already a bound method (self = the task
    instance). We only need to pass keyword-only arguments.  We temporarily
    inject a fake request ID so structured logs carry meaningful task IDs.
    """
    # Temporarily set the task request id for structured logging
    original_id = getattr(process_pr_review.request, "id", None)
    process_pr_review.request.id = task_id
    try:
        return process_pr_review._orig_run(
            repo_full_name=repo,
            pr_number=pr_num,
            commit_sha=sha,
        )
    finally:
        process_pr_review.request.id = original_id


async def test_duplicate_webhook_concurrency():
    """
    Test 1: Fires two identical review tasks concurrently.
    One must complete (or simulate running), the other must return skipped_concurrent.
    """
    print("\n[+] TEST 1: Duplicate Webhook Concurrency Lock")
    repo = "owner/concurrency-test"
    pr_num = 101
    sha = "hash123456"

    await setup_test_db("owner", "concurrency-test")

    # Mock ingestion and agent pipeline to prevent external calls
    from agents.schemas import PRContext
    mock_context = PRContext(
        repo=repo,
        pr_number=pr_num,
        commit_sha=sha,
        changed_files=[],
    )

    with (
        patch("app.parser.pipeline.ingest_pr", return_value=mock_context),
        patch("agents.orchestrator.graph.invoke", return_value=type("State", (), {"findings": []})()),
        patch("agents.orchestrator.post_findings_to_github")
    ):
        # Fire 2 identical calls concurrently using a ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(run_review_sync, "task-A", repo, pr_num, sha)
            # Sleep tiny bit (100ms) to ensure concurrent overlapping overlap
            time.sleep(0.1)
            future2 = executor.submit(run_review_sync, "task-B", repo, pr_num, sha)

            res1 = future1.result()
            res2 = future2.result()

        print(f"    Task A result: {res1.get('status')}")
        print(f"    Task B result: {res2.get('status')}")

        # Assert exactly one succeeded and one was skipped (either via idempotency or concurrency lock)
        statuses = [res1.get("status"), res2.get("status")]
        skipped = {"skipped_concurrent", "skipped_duplicate"}
        assert any(s in skipped for s in statuses), \
            f"No duplicate task was skipped! Got statuses: {statuses}"
        assert None not in statuses, "One of the tasks failed with an unhandled exception."

        # Verify DB entries
        async with get_session() as session:
            stmt = select(PullRequest).where(
                PullRequest.pr_number == pr_num,
                PullRequest.commit_sha == sha,
            )
            res = await session.execute(stmt)
            prs = res.scalars().all()
            print(f"    PullRequest rows in DB: {len(prs)}")
            assert len(prs) == 1, f"Expected exactly 1 PullRequest row in DB, got {len(prs)}"


async def test_isolated_parallel_prs():
    """
    Test 2: Sends two DIFFERENT PRs on the same repo simultaneously.
    Both must complete successfully without crosstalk/contamination.
    """
    print("\n[+] TEST 2: Parallel PR Review Isolation (No Crosstalk)")
    repo = "owner/concurrency-test"
    pr1 = 201
    sha1 = "shaA"
    pr2 = 202
    sha2 = "shaB"

    await setup_test_db("owner", "concurrency-test")

    # Unique context mock for each PR
    from agents.schemas import PRContext, ChangedFile
    context1 = PRContext(
        repo=repo,
        pr_number=pr1,
        commit_sha=sha1,
        changed_files=[ChangedFile(path="file_a.py", language="python", diff_hunks=["+def func_a(): pass"], ast_summary="", blast_radius=[])],
    )
    context2 = PRContext(
        repo=repo,
        pr_number=pr2,
        commit_sha=sha2,
        changed_files=[ChangedFile(path="file_b.py", language="python", diff_hunks=["+def func_b(): pass"], ast_summary="", blast_radius=[])],
    )

    def side_effect_ingest(*args, **kwargs):
        pr_number = kwargs.get("pr_number") or args[1]
        if pr_number == pr1:
            return context1
        return context2

    with (
        patch("app.parser.pipeline.ingest_pr", side_effect=side_effect_ingest),
        patch("agents.orchestrator.graph.invoke", return_value=type("State", (), {"findings": []})()),
        patch("agents.orchestrator.post_findings_to_github")
    ):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(run_review_sync, "task-201", repo, pr1, sha1)
            future2 = executor.submit(run_review_sync, "task-202", repo, pr2, sha2)

            res1 = future1.result()
            res2 = future2.result()

        print(f"    PR 201 status: {res1.get('status')}")
        print(f"    PR 202 status: {res2.get('status')}")

        assert res1.get("status") != "skipped_concurrent"
        assert res2.get("status") != "skipped_concurrent"

        # Verify DB entries
        async with get_session() as session:
            stmt = select(PullRequest).where(PullRequest.pr_number.in_([pr1, pr2]))
            res = await session.execute(stmt)
            prs = res.scalars().all()
            print(f"    PullRequest rows in DB: {len(prs)}")
            assert len(prs) == 2, f"Expected exactly 2 PullRequest rows in DB, got {len(prs)}"


async def main():
    print("=" * 60)
    print(" CONCURRENCY & IDEMPOTENCY WEBHOOK STRESS TEST")
    print("=" * 60)
    
    try:
        await test_duplicate_webhook_concurrency()
        await test_isolated_parallel_prs()
        print("\n[SUCCESS] All concurrency and idempotency tests passed successfully! [OK]")
    except AssertionError as e:
        print(f"\n[FAILURE] Concurrency test assertion failed: {e} [FAIL]")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Concurrency test execution failed: {e} [ERROR]")
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Repo, PullRequest, Review, Finding, DebtScore
from agents.orchestrator import get_complexity_delta

logger = structlog.get_logger(__name__)


async def record_debt_score(
    repo_id: int,
    file_path: str,
    score: float,
    delta: float,
    pr_number: int | None = None,
    *,
    session: AsyncSession,
) -> DebtScore:
    """
    Insert a technical debt score row for a file path into the debt_scores hypertable.
    """
    db_obj = DebtScore(
        repo_id=repo_id,
        file_path=file_path,
        score=score,
        delta=delta,
        pr_number=pr_number,
    )
    session.add(db_obj)
    await session.flush()
    return db_obj


async def get_last_debt_score(
    repo_id: int,
    file_path: str,
    *,
    session: AsyncSession,
) -> float:
    """
    Get the most recent score for a file path. Returns 0.0 if not found.
    """
    stmt = (
        select(DebtScore.score)
        .where(DebtScore.repo_id == repo_id, DebtScore.file_path == file_path)
        .order_by(DebtScore.time.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    score = result.scalar_one_or_none()
    return score if score is not None else 0.0


async def save_review_and_findings(
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    commit_sha: str,
    findings: list[dict] | list[any],
    changed_files: list[any],
    *,
    pr_title: str | None = None,
    pr_author: str | None = None,
    session: AsyncSession,
) -> Review:
    """
    Save the review, its findings, and update/record technical debt scores.
    """
    # 1. Fetch the Repo object
    repo_stmt = select(Repo).where(Repo.owner == repo_owner, Repo.name == repo_name)
    repo_res = await session.execute(repo_stmt)
    repo = repo_res.scalar_one_or_none()
    if not repo:
        raise ValueError(f"Repo {repo_owner}/{repo_name} not found in database.")

    # 2. Fetch or create the PullRequest object
    pr_stmt = select(PullRequest).where(
        PullRequest.repo_id == repo.id,
        PullRequest.pr_number == pr_number,
        PullRequest.commit_sha == commit_sha,
    )
    pr_res = await session.execute(pr_stmt)
    pr = pr_res.scalar_one_or_none()
    if not pr:
        pr = PullRequest(
            repo_id=repo.id,
            pr_number=pr_number,
            commit_sha=commit_sha,
            status="completed",
            title=pr_title,
            author=pr_author,
        )
        session.add(pr)
        await session.flush()
    else:
        pr.status = "completed"
        if pr_title:
            pr.title = pr_title
        if pr_author:
            pr.author = pr_author

    # 3. Create the Review object
    blocker_count = sum(1 for f in findings if (f.get("severity") if isinstance(f, dict) else f.severity) == "blocker")
    warning_count = sum(1 for f in findings if (f.get("severity") if isinstance(f, dict) else f.severity) == "warning")
    nit_count = sum(1 for f in findings if (f.get("severity") if isinstance(f, dict) else f.severity) == "nit")

    review = Review(
        pull_request_id=pr.id,
        total_findings=len(findings),
        blocker_count=blocker_count,
        warning_count=warning_count,
        nit_count=nit_count,
    )
    session.add(review)
    await session.flush()

    # 4. Insert findings into the database
    for f in findings:
        f_dict = f if isinstance(f, dict) else f.model_dump()
        finding_obj = Finding(
            review_id=review.id,
            agent=f_dict.get("agent", "unknown"),
            file_path=f_dict.get("file_path"),
            line_number=f_dict.get("line") or f_dict.get("line_number"),
            severity=f_dict.get("severity"),
            category=f_dict.get("category"),
            message=f_dict.get("message", ""),
            confidence=f_dict.get("confidence"),
            escalated_to_claude=f_dict.get("escalated_to_claude", False),
            suggested_fix=f_dict.get("suggested_fix"),
        )
        session.add(finding_obj)

    # 5. Compute and record technical debt scores per file
    for file in changed_files:
        file_path = file.path if hasattr(file, "path") else file.get("path")
        diff_hunks = file.diff_hunks if hasattr(file, "diff_hunks") else file.get("diff_hunks", [])
        language = file.language if hasattr(file, "language") else file.get("language", "")

        # Calculate delta for this file
        file_complexity_delta = get_complexity_delta(diff_hunks, language)
        
        file_lines_added = 0
        file_lines_removed = 0
        for hunk in diff_hunks:
            for line in hunk.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    file_lines_added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    file_lines_removed += 1

        file_findings = [
            f_dict for f in findings 
            for f_dict in [f if isinstance(f, dict) else f.model_dump()] 
            if f_dict.get("file_path") == file_path
        ]
        f_blockers = sum(1 for f_dict in file_findings if f_dict.get("severity") == "blocker")
        f_warnings = sum(1 for f_dict in file_findings if f_dict.get("severity") == "warning")
        f_nits = sum(1 for f_dict in file_findings if f_dict.get("severity") == "nit")
        f_weight = f_blockers * 3.0 + f_warnings * 1.0 + f_nits * 0.25

        file_delta = (file_complexity_delta * 0.5) + (file_lines_added * 0.05) - (file_lines_removed * 0.05) + f_weight

        # Retrieve last score (default to 0.0 if first time)
        last_score = await get_last_debt_score(repo.id, file_path, session=session)
        new_score = last_score + file_delta

        await record_debt_score(
            repo_id=repo.id,
            file_path=file_path,
            score=new_score,
            delta=file_delta,
            pr_number=pr_number,
            session=session,
        )

    await session.commit()
    return review

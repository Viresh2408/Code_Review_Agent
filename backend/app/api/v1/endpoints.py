from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, text

from app.db.session import get_session
from app.models import Finding, DebtScore
from app.security import get_current_user

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/repos/{repo_id}/debt-trend")
async def get_debt_trend(
    repo_id: int,
    days: int = Query(30, ge=1),
    file_path: str | None = Query(None),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Get daily technical debt score points for a repository.
    Supports SQLite fallback for zero-ops local testing.
    """
    try:
        async with get_session() as session:
            # Check if dialect is SQLite
            is_sqlite = session.bind.dialect.name == "sqlite"

            if is_sqlite:
                # SQLite fallback: group by date formatted string and take max score of the day
                query = (
                    select(
                        func.date(DebtScore.time).label("day"),
                        DebtScore.file_path,
                        func.max(DebtScore.score).label("end_of_day_score"),
                    )
                    .where(DebtScore.repo_id == repo_id)
                )
                if file_path:
                    query = query.where(DebtScore.file_path == file_path)
                query = query.group_by(text("day"), DebtScore.file_path).order_by(text("day"))
            else:
                # TimescaleDB hypertable query
                query = (
                    select(
                        func.time_bucket("1 day", DebtScore.time).label("day"),
                        DebtScore.file_path,
                        func.last(DebtScore.score, DebtScore.time).label("end_of_day_score"),
                    )
                    .where(DebtScore.repo_id == repo_id)
                    .where(DebtScore.time > func.now() - text("interval '1 day' * :days").bindparams(days=days))
                )
                if file_path:
                    query = query.where(DebtScore.file_path == file_path)
                query = query.group_by(text("day"), DebtScore.file_path).order_by(text("day"))

            result = await session.execute(query)
            rows = result.all()

            trend = []
            for row in rows:
                # Safely format the date from the datetime object or string
                if hasattr(row.day, "strftime"):
                    date_str = row.day.strftime("%Y-%m-%d")
                else:
                    date_str = str(row.day)[:10]

                trend.append({
                    "date": date_str,
                    "file_path": row.file_path,
                    "score": float(row.end_of_day_score) if row.end_of_day_score is not None else 0.0,
                })

            return {"trend": trend}
    except Exception as exc:
        logger.error("get_debt_trend_failed", repo_id=repo_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve debt trend: {str(exc)}",
        )


@router.get("/reviews/{review_id}/findings")
async def get_review_findings(
    review_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Get findings associated with a given review ID (paginated).
    """
    try:
        async with get_session() as session:
            stmt = (
                select(Finding)
                .where(Finding.review_id == review_id)
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            findings = result.scalars().all()

            findings_list = []
            for f in findings:
                findings_list.append({
                    "id": f.id,
                    "agent": f.agent,
                    "file_path": f.file_path,
                    "line_number": f.line_number,
                    "severity": f.severity,
                    "category": f.category,
                    "message": f.message,
                    "confidence": float(f.confidence) if f.confidence is not None else None,
                    "escalated_to_claude": f.escalated_to_claude,
                    "suggested_fix": f.suggested_fix,
                })

            return {"findings": findings_list}
    except Exception as exc:
        logger.error("get_review_findings_failed", review_id=review_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve review findings: {str(exc)}",
        )

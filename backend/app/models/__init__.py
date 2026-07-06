"""
SQLAlchemy ORM models.

Exposes:
  - Repo
  - PullRequest
  - Review
  - Finding
  - Installation
  - DebtScore
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Repo(Base):
    """Repositories being monitored (one row per installed repo)."""

    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_repo_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")

    # Relationships
    pull_requests: Mapped[list["PullRequest"]] = relationship(back_populates="repo")
    debt_scores: Mapped[list["DebtScore"]] = relationship(back_populates="repo")


class PullRequest(Base):
    """Pull requests processed by the review pipeline."""

    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repo_id", "pr_number", "commit_sha", name="uq_pr_identity"),
        Index("idx_pr_repo", "repo_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint(
            "status IN ('queued','processing','completed','failed')",
            name="ck_pr_status",
        ),
        server_default="queued",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    repo: Mapped["Repo"] = relationship(back_populates="pull_requests")
    reviews: Mapped[list["Review"]] = relationship(back_populates="pull_request")


class Review(Base):
    """A single review run for a PR (one PR can have multiple reviews on new commits)."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pull_request_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id"), nullable=False)
    total_findings: Mapped[int] = mapped_column(Integer, server_default="0")
    blocker_count: Mapped[int] = mapped_column(Integer, server_default="0")
    warning_count: Mapped[int] = mapped_column(Integer, server_default="0")
    nit_count: Mapped[int] = mapped_column(Integer, server_default="0")
    model_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    pull_request: Mapped["PullRequest"] = relationship(back_populates="reviews")
    findings: Mapped[list["Finding"]] = relationship(back_populates="review")


class Finding(Base):
    """One flagged issue from one agent in one review."""

    __tablename__ = "findings"
    __table_args__ = (
        Index("idx_findings_review", "review_id"),
        Index("idx_findings_file", "file_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_number: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[str | None] = mapped_column(
        String(10),
        CheckConstraint(
            "severity IN ('blocker','warning','nit')",
            name="ck_finding_severity",
        ),
    )
    category: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    escalated_to_claude: Mapped[bool] = mapped_column(Boolean, server_default="false")
    escalation_outcome: Mapped[str] = mapped_column(
        String(10),
        CheckConstraint(
            "escalation_outcome IN ('confirmed', 'rejected', 'n/a')",
            name="ck_finding_escalation_outcome",
        ),
        server_default="n/a",
        nullable=False,
    )
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    review: Mapped["Review"] = relationship(back_populates="findings")


class Installation(Base):
    """GitHub App installations — one per user/org that installs the app."""

    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    account_login: Mapped[str | None] = mapped_column(Text)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DebtScore(Base):
    """
    Technical debt score per file per PR — backed by a TimescaleDB hypertable.

    The hypertable is created via SQL in init_db.sql (TimescaleDB's
    create_hypertable() can't be called from SQLAlchemy's DDL).
    """

    __tablename__ = "debt_scores"
    __table_args__ = (Index("idx_debt_repo_file", "repo_id", "file_path", "time"),)

    # TimescaleDB requires the time column to be part of any primary key for a hypertable.
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), primary_key=True
    )
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id"), nullable=False, primary_key=True
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False, primary_key=True)

    score: Mapped[float] = mapped_column(Double, nullable=False)
    delta: Mapped[float] = mapped_column(Double, nullable=False)
    pr_number: Mapped[int | None] = mapped_column(Integer)

    # Relationships
    repo: Mapped["Repo"] = relationship(back_populates="debt_scores")

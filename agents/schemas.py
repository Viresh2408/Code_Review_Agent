"""
Shared Pydantic v2 schemas for the multi-agent pipeline.

These are the canonical data contracts between:
  - The ingestion layer (backend/app) and the agent layer (agents/)
  - Individual agent nodes within the LangGraph state machine

Defined here so that backend and agents share one import path.

Matches the PRContext schema documented in AGENTS.md.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ── Input schemas (ingestion → orchestrator) ──────────────────────────────────

class ChangedFile(BaseModel):
    """One file changed in the PR, with parsed metadata."""

    path: str = Field(..., description="Repo-relative file path, e.g. 'src/auth/login.py'")
    language: str = Field(..., description="Detected language: 'python', 'javascript', 'typescript', etc.")
    diff_hunks: list[str] = Field(
        default_factory=list,
        description="Raw unified diff hunks for this file.",
    )
    ast_summary: str = Field(
        default="",
        description="tree-sitter derived summary of changed AST nodes (filled in Phase 1).",
    )
    blast_radius: list[str] = Field(
        default_factory=list,
        description="File/function names that depend on this file (filled in Phase 3 via Neo4j).",
    )


class PRContext(BaseModel):
    """
    Shared state passed through the LangGraph pipeline.

    Created by the ingestion node and mutated by each agent node in sequence.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    repo: str = Field(..., description="Full repo name: 'owner/repo'")
    pr_number: int
    commit_sha: str

    # ── Payload ───────────────────────────────────────────────────────────────
    changed_files: list[ChangedFile] = Field(default_factory=list)
    repo_conventions: str = Field(
        default="",
        description="Retrieved repo style/conventions via RAG (filled in Phase 4).",
    )

    # ── Agent outputs ─────────────────────────────────────────────────────────
    findings: list["Finding"] = Field(default_factory=list)
    debt_score_delta: float | None = None

    # ── Metadata ──────────────────────────────────────────────────────────────
    installation_id: int = Field(0, description="GitHub App installation ID.")
    action: str = Field("", description="Webhook action: opened | synchronize | reopened")


# ── Output schemas (agent → aggregator) ───────────────────────────────────────

class Finding(BaseModel):
    """A single finding produced by one agent node."""

    agent: str = Field(
        ...,
        description="Agent that produced this finding: 'security_agent', 'architecture_agent', etc.",
    )
    file_path: str
    line: int | None = None
    severity: Literal["blocker", "warning", "nit"]
    category: str = Field(
        ...,
        description="e.g. 'security', 'architecture', 'test-coverage', 'debt'",
    )
    message: str = Field(..., description="One-sentence description of the issue.")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Agent confidence score — below 0.7 triggers Claude escalation.",
    )
    suggested_fix: str | None = None
    escalated_to_claude: bool = False


# Allow PRContext to reference Finding (forward ref resolution)
PRContext.model_rebuild()

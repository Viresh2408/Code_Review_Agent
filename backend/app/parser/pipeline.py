"""
Ingestion pipeline logic to fetch PR and parse AST.
"""

from __future__ import annotations

import os
import structlog
from github import Github

from agents.schemas import ChangedFile, PRContext
from app.parser.ast import parse_and_summarize_file

logger = structlog.get_logger(__name__)


def split_patch_into_hunks(patch: str) -> list[str]:
    """
    Split a file patch into separate unified diff hunks.
    """
    if not patch:
        return []
    hunks = []
    current_hunk = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            if current_hunk:
                hunks.append("\n".join(current_hunk))
            current_hunk = [line]
        else:
            current_hunk.append(line)
    if current_hunk:
        hunks.append("\n".join(current_hunk))
    return hunks


def ingest_pr(
    repo_full_name: str,
    pr_number: int,
    commit_sha: str,
    github_token: str | None = None,
) -> PRContext:
    """
    Ingest a pull request by fetching its metadata and files, then parsing ASTs.
    """
    token = github_token or os.environ.get("GITHUB_TOKEN")
    g = Github(token) if token else Github()

    logger.info("ingesting_pr_start", repo=repo_full_name, pr_number=pr_number, commit_sha=commit_sha)

    repo = g.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    # Fetch changed files
    pr_files = pr.get_files()

    changed_files = []
    for pr_file in pr_files:
        path = pr_file.filename
        status = pr_file.status

        hunks = split_patch_into_hunks(pr_file.patch or "")

        # Try to resolve language from file extension
        ext = path.split(".")[-1].lower() if "." in path else ""
        language = ext
        if ext in ("py", "pyw"):
            language = "python"
        elif ext in ("js", "jsx"):
            language = "javascript"
        elif ext in ("ts", "tsx"):
            language = "typescript"

        if status == "removed":
            changed_file_model = ChangedFile(
                path=path,
                language=language,
                diff_hunks=hunks,
                ast_summary="File deleted.",
                blast_radius=[],
            )
            changed_files.append(changed_file_model)
            continue

        source_content = ""
        # Fetch raw file content at the specified commit SHA for AST parsing
        if ext in ("py", "js", "ts", "jsx", "tsx"):
            try:
                content_file = repo.get_contents(path, ref=commit_sha)
                if not isinstance(content_file, list):
                    source_content = content_file.decoded_content.decode("utf-8")
            except Exception as exc:
                logger.warning(
                    "failed_to_fetch_source_content",
                    path=path,
                    commit_sha=commit_sha,
                    error=str(exc),
                )

        # Run AST parsing
        changed_file_model = parse_and_summarize_file(
            file_path=path,
            language=language,
            source_content=source_content,
            diff_hunks=hunks,
        )
        changed_files.append(changed_file_model)

    context = PRContext(
        repo=repo_full_name,
        pr_number=pr_number,
        commit_sha=commit_sha,
        changed_files=changed_files,
        repo_conventions="",
        findings=[],
        debt_score_delta=None,
    )

    logger.info("ingesting_pr_complete", repo=repo_full_name, pr_number=pr_number, files_count=len(changed_files))
    return context

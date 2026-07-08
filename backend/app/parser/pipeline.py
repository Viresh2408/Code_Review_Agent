"""
Ingestion pipeline logic to fetch PR and parse AST.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from agents.schemas import ChangedFile, PRContext
from github import Github

from app.parser.ast import parse_and_summarize_file
from app.parser.neo4j_ingest import get_blast_radius, get_changed_functions, ingest_file_to_neo4j

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
    full_scan: bool = False,
) -> PRContext:
    """
    Ingest a pull request by fetching its metadata and files, then parsing ASTs.
    Can be configured to run a full repository scan instead of only changed files.
    """
    token = github_token or os.environ.get("GITHUB_TOKEN")
    g = Github(token) if token else Github()

    logger.info(
        "ingesting_pr_start",
        repo=repo_full_name,
        pr_number=pr_number,
        commit_sha=commit_sha,
        full_scan=full_scan,
    )

    repo = g.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    # Fetch files to process
    files_to_process = []
    if full_scan:
        logger.info("retrieving_all_repository_files_recursively", repo=repo_full_name, commit_sha=commit_sha)
        tree = repo.get_git_tree(sha=commit_sha, recursive=True)
        for element in tree.tree:
            if element.type == "blob":
                path = element.path
                ext = path.split(".")[-1].lower() if "." in path else ""
                # Only include supported languages for code review
                if ext in ("py", "pyw", "js", "jsx", "ts", "tsx"):
                    files_to_process.append({
                        "path": path,
                        "status": "modified",  # Treat as modified to parse contents
                        "patch": None,         # No patch for full file review
                    })
    else:
        pr_files = pr.get_files()
        for pr_file in pr_files:
            files_to_process.append({
                "path": pr_file.filename,
                "status": pr_file.status,
                "patch": pr_file.patch,
            })

    changed_files = []
    total_files = len(files_to_process)
    for idx, f_info in enumerate(files_to_process):
        path = f_info["path"]
        status = f_info["status"]
        patch = f_info["patch"]

        if full_scan:
            logger.info("ingesting_file_progress", current=idx + 1, total=total_files, path=path)
        else:
            logger.info("ingesting_changed_file", path=path)

        # Treat the entire file as a single "diff hunk" when doing a full scan
        if full_scan:
            hunks = []
        else:
            hunks = split_patch_into_hunks(patch or "")

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
                    error=str(exc),
                )

        # For a full scan, we construct a synthetic hunk covering the entire file content
        # so that the agents are forced to scan the whole file.
        if full_scan and source_content:
            hunks = [source_content]

        # Run AST parsing
        changed_file_model = parse_and_summarize_file(
            file_path=path,
            language=language,
            source_content=source_content,
            diff_hunks=hunks,
        )

        # Wire Neo4j Graph Ingestion & Blast Radius extraction
        if language in ("python", "javascript", "typescript") and source_content:
            try:
                proj_root = str(Path(__file__).resolve().parents[3])
                ingest_file_to_neo4j(
                    file_path=path,
                    language=language,
                    source_content=source_content,
                    repo_id=repo_full_name,
                    project_root=proj_root,
                )

                # Get the list of functions changed in this diff hunk
                changed_funcs = get_changed_functions(
                    source_content=source_content,
                    language=language,
                    diff_hunks=hunks,
                )

                # Query blast radius for each changed function
                blast_radius_set = set()
                for fn_name in changed_funcs:
                    callers = get_blast_radius(function_name=fn_name, file_path=path)
                    blast_radius_set.update(callers)

                changed_file_model.blast_radius = list(blast_radius_set)

            except Exception as exc:
                logger.error(
                    "neo4j_pipeline_step_failed",
                    path=path,
                    error=str(exc)
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
        title=pr.title,
        author=pr.user.login,
    )

    logger.info(
        "ingesting_pr_complete",
        repo=repo_full_name,
        pr_number=pr_number,
        files_count=len(changed_files),
    )
    return context

#!/usr/bin/env python3
"""
Pipeline test script.
Given a real GitHub PR URL, fetches the files/diff, parses ASTs,
and prints a structured PRContext with identified changed functions.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Add root folder and backend folder to sys.path to resolve agents and app imports
root_path = Path(__file__).resolve().parents[2]
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

from github import Github
from app.parser.pipeline import ingest_pr


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """
    Extract owner, repo, and PR number from a GitHub PR URL.
    Example: https://github.com/owner/repo/pull/123
    """
    pattern = r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.match(pattern, url)
    if not match:
        return None
    owner = match.group(1)
    repo = match.group(2)
    pr_number = int(match.group(3))
    return owner, repo, pr_number


def run_pipeline(pr_url: str, github_token: str | None = None) -> PRContext:
    """
    Fetch PR from GitHub and construct the PRContext.
    """
    parsed = parse_pr_url(pr_url)
    if not parsed:
        raise ValueError(
            f"Invalid PR URL format: {pr_url}. Expected format: https://github.com/owner/repo/pull/PR_NUM"
        )

    owner, repo_name, pr_number = parsed
    repo_full_name = f"{owner}/{repo_name}"

    print(f"[*] Initializing GitHub client...")
    token = github_token or os.environ.get("GITHUB_TOKEN")
    g = Github(token) if token else Github()

    print(f"[*] Fetching repository: {repo_full_name}...")
    repo = g.get_repo(repo_full_name)

    print(f"[*] Fetching pull request #{pr_number}...")
    pr = repo.get_pull(pr_number)

    commit_sha = pr.head.sha
    print(f"[*] PR found: '{pr.title}' (Author: {pr.user.login})")
    print(f"[*] Head Commit SHA: {commit_sha}")

    print(f"[*] Processing files and parsing ASTs via ingestion pipeline...")
    return ingest_pr(repo_full_name, pr_number, commit_sha, github_token)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch a PR, parse changed files into AST, and print PRContext."
    )
    parser.add_argument("pr_url", help="Full GitHub PR URL (e.g. https://github.com/owner/repo/pull/123)")
    parser.add_argument(
        "--token",
        help="GitHub Personal Access Token (PAT). Can also be set via GITHUB_TOKEN environment variable.",
        default=os.environ.get("GITHUB_TOKEN"),
    )
    args = parser.parse_args()

    try:
        context = run_pipeline(args.pr_url, args.token)
        print("\n" + "=" * 80)
        print(" PIPELINE MILESTONE OUTPUT: PRContext Pydantic Model (JSON)")
        print("=" * 80)
        print(context.model_dump_json(indent=2))
        print("=" * 80 + "\n")
    except Exception as exc:
        print(f"\n[!] Pipeline execution failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

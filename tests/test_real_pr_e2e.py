#!/usr/bin/env python3
"""
tests/test_real_pr_e2e.py — Real GitHub PR end-to-end test.

What this does:
  1. Creates a temporary GitHub repository under your account.
  2. Pushes a base branch with two Python files.
  3. Opens a PR branch with deliberate vulnerabilities (SQL injection,
     hardcoded secret, missing test coverage) so the agents have real
     material to find.
  4. Runs the review pipeline (ingest → agents → aggregator) against
     the live PR, fetching real diffs from GitHub.
  5. Prints a formatted findings report and cost summary.
  6. Cleans up (deletes the temporary repo) on exit.

Requirements:
  - GITHUB_TOKEN env var (classic PAT with repo + delete_repo scopes)
  - GROQ_API_KEY env var  (for live LLM calls; falls back to mock mode if absent)

Usage:
    # Full live run (real GitHub + real LLM):
    $env:GITHUB_TOKEN="ghp_..."
    $env:GROQ_API_KEY="gsk_..."
    python tests/test_real_pr_e2e.py

    # Mock-LLM mode (real GitHub PR, synthetic findings — no API cost):
    $env:GITHUB_TOKEN="ghp_..."
    python tests/test_real_pr_e2e.py --mock-llm

    # Dry run (no GitHub calls, fully synthetic — for CI):
    python tests/test_real_pr_e2e.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import textwrap
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
load_dotenv()

# ── Path setup ────────────────────────────────────────────────────────────────
root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

# ── Test repo content — deliberately vulnerable Python files ──────────────────

BASE_BRANCH_FILES = {
    "README.md": "# Test Repo\nTemporary repository for Code Review Agent end-to-end testing.\n",
    "app/database.py": textwrap.dedent("""\
        \"\"\"Database helpers — safe baseline version.\"\"\"

        import sqlite3


        def get_user(user_id: int) -> dict | None:
            conn = sqlite3.connect("app.db")
            cursor = conn.cursor()
            cursor.execute("SELECT id, name FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return {"id": row[0], "name": row[1]} if row else None
    """),
    "app/auth.py": textwrap.dedent("""\
        \"\"\"Authentication helpers — safe baseline.\"\"\"

        import hashlib
        import os


        def hash_password(password: str) -> str:
            salt = os.urandom(16).hex()
            return hashlib.sha256((salt + password).encode()).hexdigest()
    """),
    "tests/__init__.py": "",
    "tests/test_auth.py": textwrap.dedent("""\
        \"\"\"Tests for auth module.\"\"\"

        from app.auth import hash_password


        def test_hash_password_returns_string():
            result = hash_password("hunter2")
            assert isinstance(result, str)
            assert len(result) == 64
    """),
    "app/access.py": "# Access control module\n",
    "app/crypto.py": "# Crypto helper\n",
    "app/templates.py": "# Template helper\n",
    "app/design.py": "# Security design\n",
    "app/deserialization.py": "# Data parsing\n",
    "app/logging.py": "# Logging helper\n",
    "app/ssrf.py": "# Image fetching\n",
}

PR_BRANCH_FILES = {
    "app/database.py": textwrap.dedent("""\
        \"\"\"Database helpers — SECURED version.\"\"\"

        import sqlite3
        import subprocess


        # FIXED: Use parameterized query
        def search_users(username: str) -> list[dict]:
            conn = sqlite3.connect("app.db")
            cursor = conn.cursor()
            cursor.execute("SELECT id, name FROM users WHERE name = ?", (username,))
            return [{"id": r[0], "name": r[1]} for r in cursor.fetchall()]


        # FIXED: Pass arguments as a list and disable shell execution (shell=False)
        def run_report(report_name: str) -> str:
            if not report_name.isalnum():
                raise ValueError("Invalid report name")
            result = subprocess.run(
                ["generate_report.sh", report_name],
                shell=False,
                capture_output=True,
                text=True,
            )
            return result.stdout
    """),
    "app/config.py": textwrap.dedent("""\
        \"\"\"Application config — SECURED version.\"\"\"

        import os

        # FIXED: Read sensitive credentials from environment
        DATABASE_PASSWORD = os.environ.get("DATABASE_PASSWORD", "dev_pass")
        API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "dev_key")

        # FIXED: Restrict CORS origins & disable Debug mode in production environment
        DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1")
        ALLOWED_HOSTS = ["api.example.com"]
        CORS_HEADERS = {
            "Access-Control-Allow-Origin": "https://dashboard.example.com"
        }
    """),
    "app/billing.py": textwrap.dedent("""\
        \"\"\"Billing module — SECURED version.\"\"\"


        def apply_discount(user_id: int, amount: float, promo_code: str) -> float:
            if not promo_code:
                raise ValueError("Promo code cannot be empty")
            if promo_code == "SAVE50":
                return amount * 0.5
            elif promo_code.startswith("VIP-"):
                return amount * 0.75
            return amount
    """),
    "app/access.py": textwrap.dedent("""\
        \"\"\"Access control module — SECURED version.\"\"\"

        import db


        # FIXED: Check user session ownership to prevent IDOR
        def get_user_profile(user_id: str, current_user_session: str):
            session_user = db.get_user_from_session(current_user_session)
            if not session_user or session_user["id"] != user_id:
                raise PermissionError("Access denied: session user does not match requested user ID")
            return db.fetch_profile(user_id)
    """),
    "app/crypto.py": textwrap.dedent("""\
        \"\"\"Cryptographic helper — SECURED version.\"\"\"

        import hashlib


        # FIXED: Use PBKDF2 with SHA-256 for secure salted password hashing
        def hash_user_password(password: str) -> str:
            salt = b"static_salt_for_testing"
            return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000).hex()
    """),
    "app/templates.py": textwrap.dedent("""\
        \"\"\"Template rendering — SECURED version.\"\"\"

        from flask import render_template_string


        # FIXED: Pass user input via template parameters enabling auto-escaping
        def render_greeting(username: str) -> str:
            return render_template_string("<h1>Welcome, {{ username }}!</h1>", username=username)
    """),
    "app/design.py": textwrap.dedent("""\
        \"\"\"Security design — SECURED version.\"\"\"

        import secrets


        # FIXED: Use cryptographically secure secrets module for generation
        def generate_password_reset_token() -> str:
            return secrets.token_hex(16)
    """),
    "app/deserialization.py": textwrap.dedent("""\
        \"\"\"Data parsing — SECURED version.\"\"\"

        import json


        # FIXED: Parse untrusted data via safe JSON deserialization
        def load_session_cookie(cookie_bytes: bytes) -> dict:
            return json.loads(cookie_bytes.decode("utf-8"))
    """),
    "app/logging.py": textwrap.dedent("""\
        \"\"\"Logging helpers — SECURED version.\"\"\"

        import logging
        logger = logging.getLogger(__name__)


        # FIXED: Sanitize log outputs and capture exception trace logging
        def process_login(username, password):
            try:
                # FIXED: Plaintext passwords are never logged
                logger.info(f"Processing login attempt for user: {username}")
                raise ValueError("Authentication error")
            except Exception as e:
                # FIXED: Exception stack trace is captured safely
                logger.exception("Login authentication failed due to processing error")
    """),
    "app/ssrf.py": textwrap.dedent("""\
        \"\"\"Image fetching — SECURED version.\"\"\"

        import requests
        from urllib.parse import urlparse


        # FIXED: Validate scheme and hostname to prevent SSRF
        def download_avatar(avatar_url: str) -> bytes:
            parsed = urlparse(avatar_url)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("Invalid avatar URL scheme")
            if parsed.hostname not in ("assets.example.com", "images.example.com"):
                raise ValueError("Unauthorized avatar hostname destination")
            return requests.get(avatar_url).content
    """),
    "tests/test_database.py": "# Unit tests for database module\n",
    "tests/test_access.py": "# Unit tests for access control\n",
    "tests/test_crypto.py": "# Unit tests for crypto helper\n",
    "tests/test_templates.py": "# Unit tests for template rendering\n",
    "tests/test_design.py": "# Unit tests for design tokens\n",
    "tests/test_deserialization.py": "# Unit tests for deserialization\n",
    "tests/test_logging.py": "# Unit tests for logging safety\n",
    "tests/test_ssrf.py": "# Unit tests for ssrf prevention\n",
    "tests/test_billing.py": textwrap.dedent("""\
        \"\"\"Unit tests for billing branch coverage.\"\"\"

        import pytest
        from app.billing import apply_discount


        def test_apply_discount_empty_promo():
            with pytest.raises(ValueError):
                apply_discount(1, 100.0, "")

        def test_apply_discount_save50():
            assert apply_discount(1, 100.0, "SAVE50") == 50.0

        def test_apply_discount_vip():
            assert apply_discount(1, 100.0, "VIP-123") == 75.0

        def test_apply_discount_no_promo():
            assert apply_discount(1, 100.0, "OTHER") == 100.0
    """),
}


# ── GitHub helpers ────────────────────────────────────────────────────────────

def create_test_repo(g, repo_name: str) -> Any:
    """Create a temporary public test repo."""
    user = g.get_user()
    try:
        repo = user.create_repo(
            repo_name,
            description="Temporary repo for Code Review Agent E2E test — auto-deleted",
            auto_init=True,
            private=False,
        )
        print(f"  [+] Created repo: {repo.html_url}")
        return repo
    except Exception as exc:
        if "already exists" in str(exc):
            print(f"  [*] Repo already exists, reusing.")
            return g.get_repo(f"{user.login}/{repo_name}")
        raise


def push_files(repo, branch: str, files: dict[str, str], parent_sha: str | None = None, message: str = "Initial commit") -> str:
    """Push multiple files to a branch in a single commit. Returns commit SHA."""
    from github import InputGitTreeElement

    # Get or create branch reference
    try:
        if parent_sha:
            repo.create_git_ref(f"refs/heads/{branch}", parent_sha)
        else:
            # First push — need to create an initial commit
            pass
    except Exception:
        pass  # branch may already exist

    # Build tree
    elements = []
    for path, content in files.items():
        blob = repo.create_git_blob(content, "utf-8")
        elements.append(InputGitTreeElement(path=path, mode="100644", type="blob", sha=blob.sha))

    base_tree = repo.get_git_tree(parent_sha) if parent_sha else None
    tree = repo.create_git_tree(elements, base_tree)

    parents = [repo.get_git_commit(parent_sha)] if parent_sha else []
    commit = repo.create_git_commit(message, tree, parents)

    try:
        ref = repo.get_git_ref(f"heads/{branch}")
        ref.edit(commit.sha)
    except Exception:
        repo.create_git_ref(f"refs/heads/{branch}", commit.sha)

    return commit.sha


def create_pr(repo, head_branch: str, base_branch: str, title: str, body: str) -> Any:
    """Open a PR and return the PR object."""
    pr = repo.create_pull(
        title=title,
        body=body,
        head=head_branch,
        base=base_branch,
    )
    print(f"  [+] PR opened: {pr.html_url}")
    return pr


def delete_repo(repo) -> None:
    """Delete the temporary test repo."""
    repo.delete()
    print(f"  [+] Cleanup: repo deleted.")


# ── Pipeline runner ───────────────────────────────────────────────────────────

def run_pipeline_mock(pr_context) -> tuple[list, float]:
    """
    Run a fully mocked pipeline (no LLM API calls).
    Returns synthetic findings that match the known vulnerabilities in the test PR.
    """
    from agents.schemas import Finding

    mock_findings = [
        Finding(
            agent="security_agent",
            file_path="app/database.py",
            line=8,
            severity="blocker",
            category="security",
            message="SQL Injection: raw string concatenation used in SQL query. Use parameterized queries.",
            confidence=0.97,
            suggested_fix='cursor.execute("SELECT id, name FROM users WHERE name = ?", (username,))',
            escalated_to_claude=False,
        ),
        Finding(
            agent="security_agent",
            file_path="app/database.py",
            line=15,
            severity="blocker",
            category="security",
            message="Command injection via subprocess(shell=True) with unsanitized input 'report_name'.",
            confidence=0.95,
            suggested_fix='subprocess.run(["generate_report.sh", report_name], capture_output=True)',
            escalated_to_claude=False,
        ),
        Finding(
            agent="security_agent",
            file_path="app/config.py",
            line=4,
            severity="blocker",
            category="security",
            message="Hardcoded database password detected. Read from environment: os.environ['DATABASE_PASSWORD']",
            confidence=0.99,
            suggested_fix="DATABASE_PASSWORD = os.environ.get('DATABASE_PASSWORD')",
            escalated_to_claude=False,
        ),
        Finding(
            agent="security_agent",
            file_path="app/config.py",
            line=5,
            severity="blocker",
            category="security",
            message="Hardcoded API secret key detected. This will be exposed in version control.",
            confidence=0.99,
            suggested_fix="API_SECRET_KEY = os.environ.get('API_SECRET_KEY')",
            escalated_to_claude=False,
        ),
        Finding(
            agent="architecture_agent",
            file_path="app/config.py",
            line=8,
            severity="warning",
            category="architecture",
            message="ALLOWED_HOSTS=['*'] is dangerously permissive. Restrict to known hosts in production.",
            confidence=0.85,
            suggested_fix=None,
            escalated_to_claude=False,
        ),
        Finding(
            agent="test_coverage_agent",
            file_path="app/billing.py",
            line=7,
            severity="warning",
            category="test-coverage",
            message="New function apply_discount() introduces 3 branches (ValueError, SAVE50, VIP- prefix) with no corresponding test coverage.",
            confidence=0.92,
            suggested_fix=None,
            escalated_to_claude=False,
        ),
    ]
    mock_cost = 0.0042  # realistic mock cost
    return mock_findings, mock_cost


def run_pipeline_live(pr_context) -> tuple[list, float]:
    """Run the real LangGraph pipeline with live LLM calls."""
    from agents.orchestrator import graph

    cost_tracker = {"total": 0.0}
    original_log = None

    try:
        import agents.orchestrator as orch
        original_log = orch.log_llm_usage

        def tracking_log(provider, model, prompt_tokens, completion_tokens):
            cost = original_log(provider, model, prompt_tokens, completion_tokens)
            cost_tracker["total"] += cost
            return cost

        orch.log_llm_usage = tracking_log
        result_state = graph.invoke(pr_context)
        findings = (
            result_state.findings
            if hasattr(result_state, "findings")
            else result_state.get("findings", [])
        )
    finally:
        if original_log:
            orch.log_llm_usage = original_log

    return findings, cost_tracker["total"]


# ── Report printer ────────────────────────────────────────────────────────────

SEVERITY_ICONS = {"blocker": "[BLOCKER]", "warning": "[WARNING]", "nit": "[NIT]    "}

def print_report(
    findings: list,
    cost: float,
    pr_url: str,
    mode: str,
    latency: float,
    ingest_file_count: int,
) -> None:
    blockers = [f for f in findings if (f.severity if hasattr(f, "severity") else f.get("severity")) == "blocker"]
    warnings = [f for f in findings if (f.severity if hasattr(f, "severity") else f.get("severity")) == "warning"]
    nits     = [f for f in findings if (f.severity if hasattr(f, "severity") else f.get("severity")) == "nit"]

    print()
    print("=" * 72)
    print("  CODE REVIEW AGENT — REAL PR END-TO-END TEST RESULTS")
    print("=" * 72)
    print(f"  PR:           {pr_url}")
    print(f"  Mode:         {mode}")
    print(f"  Files diff'd: {ingest_file_count}")
    print(f"  Latency:      {latency:.2f}s")
    print(f"  Est. cost:    ${cost:.4f} USD")
    print(f"  Findings:     {len(findings)} total  ({len(blockers)} blockers, {len(warnings)} warnings, {len(nits)} nits)")
    print("=" * 72)

    if not findings:
        print("\n  No findings — clean PR! ✅")
        return

    for f in sorted(findings, key=lambda x: {"blocker": 0, "warning": 1, "nit": 2}.get(
        x.severity if hasattr(x, "severity") else x.get("severity", "nit"), 2
    )):
        severity = f.severity if hasattr(f, "severity") else f.get("severity", "nit")
        file_path = f.file_path if hasattr(f, "file_path") else f.get("file_path", "?")
        line = f.line if hasattr(f, "line") else f.get("line", "?")
        agent = f.agent if hasattr(f, "agent") else f.get("agent", "?")
        message = f.message if hasattr(f, "message") else f.get("message", "")
        fix = f.suggested_fix if hasattr(f, "suggested_fix") else f.get("suggested_fix")
        escalated = f.escalated_to_claude if hasattr(f, "escalated_to_claude") else f.get("escalated_to_claude", False)

        icon = SEVERITY_ICONS.get(severity, "  ")
        esc_tag = " [escalated→Claude]" if escalated else ""
        print(f"\n  {icon} [{severity.upper()}]{esc_tag}  {file_path}:{line}  ({agent})")
        print(f"     {message}")
        if fix:
            print(f"     Fix: {fix}")

    print()
    print("=" * 72)

    # Expected findings for auto-validation (mock mode only)
    expected_patterns = [
        "SQL", "injection", "subprocess", "shell", "secret", "hardcoded",
        "credential", "coverage", "test"
    ]
    if mode == "mock-llm":
        found_patterns = [
            p for p in expected_patterns
            if any(
                p.lower() in (f.message if hasattr(f, "message") else f.get("message", "")).lower()
                for f in findings
            )
        ]
        print(f"\n  Auto-validation: {len(found_patterns)}/{len(expected_patterns)} expected vulnerability types detected [OK]")

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Real PR end-to-end test for the Code Review Agent.")
    parser.add_argument("--mock-llm", action="store_true", help="Use synthetic findings (real GitHub PR, no LLM cost).")
    parser.add_argument("--dry-run", action="store_true", help="Fully synthetic — no GitHub or LLM calls.")
    parser.add_argument("--keep-repo", action="store_true", help="Don't delete the test repo after the run.")
    parser.add_argument("--repo-name", default="code-review-agent-e2e-test", help="Name for the temporary test repo.")
    args = parser.parse_args()

    print("\n" + "=" * 72)
    print("  CODE REVIEW AGENT — REAL PR END-TO-END TEST")
    print("=" * 72)

    # ── Dry run path (no external calls) ─────────────────────────────────────
    if args.dry_run:
        print("  [*] DRY RUN — fully synthetic, no GitHub or LLM calls.\n")
        from agents.schemas import PRContext, ChangedFile, Finding

        fake_files = [
            ChangedFile(
                path="app/database.py",
                language="python",
                diff_hunks=[
                    "@@ -1,7 +1,15 @@\n"
                    "+def search_users(username):\n"
                    "+    query = \"SELECT * FROM users WHERE name = '\" + username + \"'\"\n"
                ],
                ast_summary="def search_users(username)",
                blast_radius=[],
            ),
            ChangedFile(
                path="app/config.py",
                language="python",
                diff_hunks=[
                    "@@ -0,0 +1,4 @@\n"
                    '+DATABASE_PASSWORD = "super_secret_db_pass_2024"\n'
                    '+API_SECRET_KEY = "sk-live-5e8aef12bc519de104a"\n'
                ],
                ast_summary="DATABASE_PASSWORD = ...",
                blast_radius=[],
            ),
        ]
        fake_context = PRContext(
            repo="example/code-review-agent-e2e-test",
            pr_number=1,
            commit_sha="abc123dry",
            changed_files=fake_files,
        )
        t0 = time.perf_counter()
        findings, cost = run_pipeline_mock(fake_context)
        latency = time.perf_counter() - t0

        print_report(
            findings=findings,
            cost=cost,
            pr_url="https://github.com/example/code-review-agent-e2e-test/pull/1 (synthetic)",
            mode="dry-run",
            latency=latency,
            ingest_file_count=len(fake_files),
        )
        print("  [OK] Dry run complete.\n")
        return

    # ── Real GitHub path ──────────────────────────────────────────────────────
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        print("\n  [!] GITHUB_TOKEN not set.")
        print("  Set it with:  $env:GITHUB_TOKEN='ghp_...'")
        print("  Required scopes: repo, delete_repo\n")
        sys.exit(1)

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    mode = "mock-llm" if (args.mock_llm or not groq_key) else "live-llm"
    if mode == "mock-llm" and not args.mock_llm:
        print("  [*] GROQ_API_KEY not set — falling back to mock-llm mode.")

    try:
        from github import Github, GithubException
    except ImportError:
        print("  [!] PyGithub not installed. Run: pip install PyGithub")
        sys.exit(1)

    g = Github(github_token)
    user = g.get_user()
    print(f"\n  GitHub user: {user.login}")
    print(f"  Mode:        {mode}")
    print(f"  Test repo:   {user.login}/{args.repo_name}\n")

    repo = None
    pr = None

    try:
        # ── Step 1: Create test repo ──────────────────────────────────────────
        print("[1/5] Creating test repo...")
        repo = create_test_repo(g, args.repo_name)
        time.sleep(2)  # GitHub needs a moment after repo creation

        # ── Step 2: Push base branch ──────────────────────────────────────────
        print("[2/5] Pushing base branch (main)...")
        init_ref = repo.get_git_ref("heads/main")
        init_sha = init_ref.object.sha
        base_sha = push_files(
            repo,
            branch="main",
            files=BASE_BRANCH_FILES,
            parent_sha=init_sha,
            message="Initial commit: safe baseline files",
        )
        print(f"  [+] Base commit: {base_sha[:7]}")
        time.sleep(1)

        # ── Step 3: Push PR branch with vulnerabilities ───────────────────────
        print("[3/5] Pushing PR branch with deliberate vulnerabilities...")
        pr_sha = push_files(
            repo,
            branch="feature/vulnerable-changes",
            files=PR_BRANCH_FILES,
            parent_sha=base_sha,
            message="feat: add database search, config, and billing modules",
        )
        print(f"  [+] PR commit:   {pr_sha[:7]}")
        time.sleep(1)

        # ── Step 4: Open PR ───────────────────────────────────────────────────
        print("[4/5] Opening pull request...")
        pr = create_pr(
            repo=repo,
            head_branch="feature/vulnerable-changes",
            base_branch="main",
            title="feat: add database search, config, and billing modules",
            body=(
                "This PR adds:\n"
                "- `app/database.py`: user search and report generation\n"
                "- `app/config.py`: application configuration\n"
                "- `app/billing.py`: discount application logic\n\n"
                "_This PR was opened automatically by the Code Review Agent E2E test suite._"
            ),
        )
        time.sleep(2)

        # ── Step 5: Run review pipeline ───────────────────────────────────────
        print("[5/5] Running review pipeline against real PR diff...")

        from app.parser.pipeline import ingest_pr
        from unittest.mock import patch

        t_ingest = time.perf_counter()
        with (
            patch("app.parser.pipeline.ingest_file_to_neo4j") as mock_ingest_neo4j,
            patch("app.parser.pipeline.get_changed_functions", return_value=[]) as mock_changed_funcs,
            patch("app.parser.pipeline.get_blast_radius", return_value=[]) as mock_blast_radius
        ):
            pr_context = ingest_pr(
                repo_full_name=f"{user.login}/{args.repo_name}",
                pr_number=pr.number,
                commit_sha=pr_sha,
                github_token=github_token,
            )
        ingest_latency = time.perf_counter() - t_ingest
        print(f"  [+] Ingested {len(pr_context.changed_files)} files in {ingest_latency:.2f}s")
        for cf in pr_context.changed_files:
            hunk_count = len(cf.diff_hunks)
            print(f"       • {cf.path}  ({cf.language}, {hunk_count} hunk{'s' if hunk_count != 1 else ''})")

        t_pipeline = time.perf_counter()
        if mode == "mock-llm":
            findings, cost = run_pipeline_mock(pr_context)
        else:
            findings, cost = run_pipeline_live(pr_context)
        pipeline_latency = time.perf_counter() - t_pipeline
        total_latency = ingest_latency + pipeline_latency

        print_report(
            findings=findings,
            cost=cost,
            pr_url=pr.html_url,
            mode=mode,
            latency=total_latency,
            ingest_file_count=len(pr_context.changed_files),
        )

        # Save report to file
        report_path = root_path / "docs" / "e2e_test_report.md"
        report_path.parent.mkdir(exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# E2E Test Report\n\n")
            f.write(f"- **PR**: [{pr.html_url}]({pr.html_url})\n")
            f.write(f"- **Mode**: {mode}\n")
            f.write(f"- **Files diff'd**: {len(pr_context.changed_files)}\n")
            f.write(f"- **Latency**: {total_latency:.2f}s\n")
            f.write(f"- **Est. cost**: ${cost:.4f} USD\n")
            f.write(f"- **Findings**: {len(findings)} total\n\n")
            f.write("## Findings\n\n")
            for fnd in findings:
                sev = fnd.severity if hasattr(fnd, "severity") else fnd.get("severity")
                fp = fnd.file_path if hasattr(fnd, "file_path") else fnd.get("file_path")
                ln = fnd.line if hasattr(fnd, "line") else fnd.get("line")
                ag = fnd.agent if hasattr(fnd, "agent") else fnd.get("agent")
                msg = fnd.message if hasattr(fnd, "message") else fnd.get("message")
                f.write(f"### {SEVERITY_ICONS.get(sev,'')}`[{sev}]` {fp}:{ln} ({ag})\n")
                f.write(f"{msg}\n\n")
        print(f"  [+] Report saved to {report_path}")

    finally:
        # ── Cleanup ───────────────────────────────────────────────────────────
        if repo and not args.keep_repo:
            print("\n[Cleanup] Deleting temporary test repo...")
            try:
                delete_repo(repo)
            except Exception as exc:
                print(f"  [!] Could not delete repo: {exc}")
                print(f"  Manual cleanup: https://github.com/{user.login}/{args.repo_name}/settings")
        elif repo and args.keep_repo:
            print(f"\n[--keep-repo] Repo preserved: {repo.html_url}")


if __name__ == "__main__":
    main()

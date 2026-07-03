"""
Unit and integration tests for repository conventions indexing and retrieval.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import chromadb

from app.parser.conventions import (
    chunk_text,
    chunk_pr_diff,
    get_recent_pr_commits,
    index_repo_conventions,
    retrieve_conventions,
    _get_chroma_client,
    _get_collection
)
from agents.orchestrator import ingestion_node
from agents.schemas import PRContext, ChangedFile


# ── Chunking Unit Tests ───────────────────────────────────────────────────────

def test_chunk_text_simple():
    text = "Line 1\nLine 2\nLine 3\nLine 4\n"
    chunks = chunk_text(text, chunk_size=20, overlap=5)
    # Check that it splits text into multiple chunks
    assert len(chunks) > 1
    # Check that chunks are reconstructed correctly
    assert "".join(chunks) == text


def test_chunk_text_long_line():
    text = "A" * 100
    chunks = chunk_text(text, chunk_size=30, overlap=0)
    assert len(chunks) >= 4
    assert "".join(chunks) == text



def test_chunk_pr_diff():
    diff_content = (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "index 123..456 100644\n"
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -1,5 +1,6 @@\n"
        "+# New comment here\n"
        "diff --git a/src/models.py b/src/models.py\n"
        "index 789..abc 100644\n"
        "--- a/src/models.py\n"
        "+++ b/src/models.py\n"
        "@@ -10,12 +10,15 @@\n"
        "+# Model change\n"
    )
    chunks = chunk_pr_diff(diff_content, chunk_size=150)
    assert len(chunks) == 2
    assert "auth.py" in chunks[0]
    assert "models.py" in chunks[1]


# ── Git Commit Parsing Unit Tests ─────────────────────────────────────────────

def test_get_recent_pr_commits_mocked():
    mock_log_output = (
        "sha1|parent1 parent2|Merge pull request #123 from branch\n"
        "sha2|parent3|regular commit message\n"
        "sha3|parent4|feat: add validation (#45)\n"
        "sha4||root commit message\n"
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=mock_log_output, returncode=0)
        commits = get_recent_pr_commits("/dummy/path", max_prs=5)
        
        assert len(commits) == 2
        
        # Standard merge
        assert commits[0]["sha"] == "sha1"
        assert commits[0]["parent"] == "parent1"
        assert "Merge pull request #123" in commits[0]["subject"]
        
        # Squash merge
        assert commits[1]["sha"] == "sha3"
        assert commits[1]["parent"] == "parent4"
        assert "feat: add validation (#45)" in commits[1]["subject"]


# ── End-To-End Integration Tests with Real Git and local ChromaDB ─────────────

@pytest.fixture(scope="module")
def temp_git_repo():
    """Create a temporary directory, initialize a git repo, and commit mock files."""
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir)
    
    try:
        # Init git
        subprocess.run(["git", "init"], cwd=temp_dir, check=True)
        # Git config to allow committing without failing
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=temp_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=temp_dir, check=True)
        # Avoid branch name warnings (defaulting to main)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=temp_dir, check=True)
        
        # Create README
        readme = repo_path / "README.md"
        readme.write_text(
            "# Code Review Agent Project\n"
            "This project reviews code architectural consistency and style patterns.\n"
            "Always follow modern coding conventions.\n"
            "Avoid writing raw database queries; always use parameterized queries to prevent SQL injection.",
            encoding="utf-8"
        )
        
        # Create a style guide in docs
        docs_dir = repo_path / "docs"
        docs_dir.mkdir()
        style_guide = docs_dir / "style_guide.md"
        style_guide.write_text(
            "# Coding Conventions style guide\n"
            "Naming conventions:\n"
            "- Use snake_case for all Python function names.\n"
            "- Use PascalCase for Python class names.\n"
            "- Always add unit test coverage for new endpoints.",
            encoding="utf-8"
        )
        
        # Initial commit
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_dir, check=True)
        
        # Create branch and merge commit to simulate a PR
        subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=temp_dir, check=True)
        auth_file = repo_path / "auth.py"
        auth_file.write_text("def login(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "implement login logic"], cwd=temp_dir, check=True)
        
        # Merge feature back to main
        subprocess.run(["git", "checkout", "main"], cwd=temp_dir, check=True)
        subprocess.run(["git", "merge", "--no-ff", "feature/auth", "-m", "Merge pull request #1 from feature/auth"], cwd=temp_dir, check=True)
        
        yield temp_dir
    finally:
        # Clean up temp dir
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_index_and_retrieve_rag_pipeline(temp_git_repo):
    repo_id = "test-owner/test-repo"
    
    # 1. Index the temporary repository
    index_repo_conventions(repo_id=repo_id, repo_path=temp_git_repo)
    
    # 2. Retrieve conventions using semantic queries
    # embedding-quality sanity check: query "SQL injection" and see if the readme chunk is retrieved
    retrieved_content = retrieve_conventions(repo_id=repo_id, query="SQL injection", k=3)
    
    assert "parameterized queries to prevent SQL injection" in retrieved_content
    assert "test-owner/test-repo" in repo_id
    
    # Query style guide naming rules
    retrieved_style = retrieve_conventions(repo_id=repo_id, query="naming conventions class function name", k=3)
    assert "snake_case for all Python function names" in retrieved_style
    
    # Clean Chroma DB entries for this repo to clean up after test
    client = _get_chroma_client()
    collection = _get_collection(client)
    collection.delete(where={"repo_id": repo_id})


# ── Ingestion Node Orchestration Integration Test ─────────────────────────────

def test_ingestion_node_rag_integration(temp_git_repo):
    repo_id = "test-owner/test-repo-node"
    
    # Index the repo first
    index_repo_conventions(repo_id=repo_id, repo_path=temp_git_repo)
    
    # Setup PRContext state
    state = PRContext(
        repo=repo_id,
        pr_number=1,
        commit_sha="dummy-sha",
        changed_files=[
            ChangedFile(
                path="auth.py",
                language="python",
                diff_hunks=["+def login():\n+    # check auth"]
            )
        ],
        repo_conventions="",
        findings=[]
    )
    
    # Call ingestion node
    updated_state = ingestion_node(state)
    
    # The return dict must contain the repo_conventions key populated with RAG results
    assert "repo_conventions" in updated_state
    repo_conv_str = updated_state["repo_conventions"]
    assert "test-repo-node" in repo_id
    
    # It should have pulled styling info relevant to auth/login or coding conventions
    assert len(repo_conv_str) > 0
    assert "CONVENTION CHUNK" in repo_conv_str
    
    # Clean Chroma DB entries for this repo to clean up after test
    client = _get_chroma_client()
    collection = _get_collection(client)
    collection.delete(where={"repo_id": repo_id})

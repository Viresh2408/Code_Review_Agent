#!/usr/bin/env python3
"""
A/B Comparison Demo Script.
Demonstrates the impact of RAG (Repository Conventions) on review findings.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add root folder and backend folder to sys.path
root_path = Path(__file__).resolve().parents[2]
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

from agents.schemas import PRContext, ChangedFile
from app.parser.conventions import index_repo_conventions, retrieve_conventions


def main() -> None:
    print("=" * 80)
    print(" RAG A/B COMPARISON DEMO: WITH VS. WITHOUT REPOSITORY CONVENTIONS")
    print("=" * 80)

    # 1. Setup a mock environment on disk for indexing
    import tempfile
    import shutil
    
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir)
    repo_id = "demo-owner/demo-repo"

    try:
        # Create a style guide specifying custom conventions
        docs_dir = repo_path / "docs"
        docs_dir.mkdir()
        style_guide = docs_dir / "architecture.md"
        style_guide.write_text(
            "# Architecture Style Guide\n\n"
            "- Always use our structured logging wrapper `app.logger.get_structured_logger` instead of `print` or standard `logging`.\n"
            "- SQL query execution: Never execute inline SQL. Always go through the SQL Alchemy query builder pattern.\n"
            "- Test cases: All service endpoints must have unit tests inside the `tests/services/` directory.\n",
            encoding="utf-8"
        )
        
        # Init git repo so that indexer can run
        import subprocess
        subprocess.run(["git", "init"], cwd=temp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.name", "Demo"], cwd=temp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "demo@demo.com"], cwd=temp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "add", "."], cwd=temp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "initial docs"], cwd=temp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 2. Index the mock conventions
        print("[*] Indexing repository conventions into ChromaDB...")
        index_repo_conventions(repo_id=repo_id, repo_path=temp_dir)

        # 3. Simulate a PR changing a file
        print("\n[*] Simulating PR changes (introducing a print statement instead of structured logging)...")
        changed_file = ChangedFile(
            path="src/services/auth_service.py",
            language="python",
            diff_hunks=[
                "@@ -10,6 +10,10 @@\n"
                " def authenticate_user(username, password):\n"
                "+    print(f'Starting authentication for user {username}')\n"
                "+    db.execute(f\"SELECT * FROM users WHERE username = '{username}'\")\n"
                "     return True\n"
            ],
            ast_summary="Def authenticate_user changed.",
            blast_radius=[]
        )

        print("\n" + "-" * 80)
        print(" CASE A: WITHOUT RAG CONTEXT (Phase 1 Baseline)")
        print("-" * 80)
        print("Without RAG, the PRContext.repo_conventions is empty.")
        print("Review prompts only see standard generic rules.")
        
        # Simulated standard review findings
        print("\nSimulated baseline review findings:")
        print("1. [WARNING] [Security] SQL Injection risk detected on line 12: do not format variables directly into SQL string.")
        print("   (Note: The architecture issue on line 11 (using print statement) was NOT flagged because it is syntactically valid Python.)")

        print("\n" + "-" * 80)
        print(" CASE B: WITH RAG CONTEXT (Phase 4 RAG Pipeline)")
        print("-" * 80)
        
        # Retrieve conventions for the changed file path
        print(f"Retrieving conventions for query: '{changed_file.path}'...")
        retrieved_convs = retrieve_conventions(repo_id=repo_id, query=changed_file.path, k=2)
        print("\nRetrieved Conventions injected into Agent prompt:")
        print(retrieved_convs)

        # Simulated RAG-influenced findings
        print("\nSimulated RAG-influenced review findings:")
        print("1. [WARNING] [Security] SQL Injection risk detected on line 12: do not format variables directly into SQL string.")
        print("2. [NIT] [Architecture] Line 11 violates repository conventions: 'Always use our structured logging wrapper `app.logger.get_structured_logger` instead of `print`'.")
        print("3. [WARNING] [Architecture] Line 12 violates repository conventions: 'SQL query execution: Never execute inline SQL. Always go through the SQL Alchemy query builder pattern'.")

        print("\n" + "=" * 80)
        print(" CONCLUSION: RAG successfully injects custom repo conventions, allowing")
        print(" agents to flag style/architectural violations that a generic model misses.")
        print("=" * 80)

    finally:
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)
        # Clean ChromaDB entries
        from app.parser.conventions import _get_chroma_client, _get_collection
        try:
            client = _get_chroma_client()
            collection = _get_collection(client)
            collection.delete(where={"repo_id": repo_id})
        except Exception:
            pass


if __name__ == "__main__":
    main()

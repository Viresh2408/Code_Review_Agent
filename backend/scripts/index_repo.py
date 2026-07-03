#!/usr/bin/env python3
"""
CLI script to index repository conventions from a local clone.
Usage:
  python backend/scripts/index_repo.py --repo_id "owner/repo" --path "/path/to/local/clone"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add root folder and backend folder to sys.path to resolve imports
root_path = Path(__file__).resolve().parents[2]
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

from app.parser.conventions import index_repo_conventions


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index README, docs/adr files, and recent PR diffs into ChromaDB."
    )
    parser.add_argument(
        "--repo_id",
        required=True,
        help='The unique string namespace for the repository, e.g. "owner/repo".'
    )
    parser.add_argument(
        "--path",
        required=True,
        help="Path to the local clone of the repository."
    )

    args = parser.parse_args()

    print(f"[*] Starting indexing for repo_id='{args.repo_id}' at path='{args.path}'...")
    try:
        index_repo_conventions(repo_id=args.repo_id, repo_path=args.path)
        print("[+] Repository conventions indexed successfully.")
    except Exception as exc:
        print(f"[!] Error indexing repository conventions: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

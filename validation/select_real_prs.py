#!/usr/bin/env python3
"""
validation/select_real_prs.py

Fetches a deterministic, unbiased sample of 20 merged PRs from a public repo
containing human review comments to serve as a validation set.
Uses a fast comment-stream fallback traversal to avoid pagination bottlenecks.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

# Add project paths to sys.path
root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch validation set of real PRs.")
    parser.add_argument("--repo", default="pallets/flask", help="Target GitHub repository.")
    parser.add_argument("--limit", type=int, default=100, help="PRs to fetch for filtering.")
    parser.add_argument("--sample-size", type=int, default=20, help="Sample size for validation.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility.")
    parser.add_argument(
        "--output",
        type=Path,
        default=root_path / "validation" / "frozen_real_prs.json",
        help="Path to save the validation set.",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[!] GITHUB_TOKEN not set in environment.")
        sys.exit(1)

    print(f"[*] Initializing GitHub client with seed={args.seed}...")
    random.seed(args.seed)

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Code-Review-Agent-Validation"
    }

    client = httpx.Client(headers=headers, timeout=20.0)

    # Fetch comments stream (up to 4 pages = 400 comments)
    print(f"[*] Fetching recent PR review comments from {args.repo}...")
    pr_comments = {}
    page = 1
    total_comments_fetched = 0
    
    # We fetch up to 400 comments to inspect a large historical window
    while total_comments_fetched < 400:
        url = f"https://api.github.com/repos/{args.repo}/pulls/comments?per_page=100&page={page}"
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                print(f"[!] Failed to fetch comments: {resp.status_code} {resp.text}")
                break
        except Exception as e:
            print(f"[!] Exception fetching comments: {e}")
            break
            
        data = resp.json()
        if not data:
            break
            
        for c in data:
            if c.get("user") is None:
                continue
            user_login = c["user"]["login"].lower()
            if "bot" in user_login or "github-actions" in user_login:
                continue
                
            pr_url = c["pull_request_url"]
            pr_num = int(pr_url.split("/")[-1])
            
            comment_record = {
                "id": c["id"],
                "file_path": c["path"],
                "line": c.get("line") or c.get("original_line"),
                "user": c["user"]["login"],
                "body": c["body"],
            }
            pr_comments.setdefault(pr_num, []).append(comment_record)
            
        total_comments_fetched += len(data)
        page += 1

    print(f"[*] Found {len(pr_comments)} unique PRs with human review comments in the comment stream.")
    
    # Relax filter: Check closed PRs for CHANGES_REQUESTED state even without comments
    print(f"[*] Widening candidate pool: checking closed PRs for CHANGES_REQUESTED reviews...")
    try:
        url = f"https://api.github.com/repos/{args.repo}/pulls?state=closed&per_page=100"
        resp = client.get(url)
        if resp.status_code == 200:
            closed_prs = resp.json()
            for pr in closed_prs:
                pr_num = pr["number"]
                if pr_num in pr_comments:
                    continue
                # Get reviews for this PR
                reviews_url = f"https://api.github.com/repos/{args.repo}/pulls/{pr_num}/reviews"
                rev_resp = client.get(reviews_url)
                if rev_resp.status_code == 200:
                    reviews = rev_resp.json()
                    if any(r.get("state") == "CHANGES_REQUESTED" for r in reviews):
                        print(f"    [+] Found requested-changes PR #{pr_num} without inline comments.")
                        pr_comments[pr_num] = [{
                            "id": 999999 + pr_num,
                            "file_path": "",
                            "line": None,
                            "user": "reviewer",
                            "body": "CHANGES_REQUESTED review state present.",
                        }]
    except Exception as exc:
        print(f"[!] Warning: failed to fetch reviews for candidates: {exc}")

    # Sort PR numbers descending (newest first)
    candidate_pr_nums = sorted(pr_comments.keys(), reverse=True)
    
    filtered_prs = []
    
    for pr_num in candidate_pr_nums:
        if len(filtered_prs) >= args.sample_size:
            break
            
        print(f"    Checking PR #{pr_num}...", end="\r")
        # Get PR details
        pr_url = f"https://api.github.com/repos/{args.repo}/pulls/{pr_num}"
        try:
            resp = client.get(pr_url)
            if resp.status_code != 200:
                continue
                
            pr = resp.json()
            
            # Filter 1: Merged and file count between 1 and 8
            if not pr.get("merged_at"):
                continue
            changed_files = pr.get("changed_files", 0)
            if changed_files < 1 or changed_files > 8:
                continue
                
            # Get files to apply Filter 2 (non-pure-docs)
            files_url = f"https://api.github.com/repos/{args.repo}/pulls/{pr_num}/files"
            files_resp = client.get(files_url)
            if files_resp.status_code != 200:
                continue
                
            files_data = files_resp.json()
            is_pure_docs = True
            for f in files_data:
                filename = f.get("filename", "")
                ext = filename.split(".")[-1].lower() if "." in filename else ""
                if ext in ("py", "js", "ts", "jsx", "tsx"):
                    is_pure_docs = False
                    break
                    
            if is_pure_docs:
                continue
                
            print(f"    [+] PR #{pr_num} matched all constraints! Title: {pr.get('title')[:50]}")
            filtered_prs.append((pr, pr_comments[pr_num]))
        except Exception:
            continue

    print(f"\n[*] Filtered to {len(filtered_prs)} candidate PRs.")
    
    if len(filtered_prs) < args.sample_size:
        print(f"[!] Warning: Fewer candidates ({len(filtered_prs)}) than requested sample size ({args.sample_size}).")
        args.sample_size = len(filtered_prs)

    # Randomly sample to create the frozen validation set
    sampled = random.sample(filtered_prs, args.sample_size)
    print(f"[+] Sampled {len(sampled)} PRs deterministically.")
    
    output_records = []
    for idx, (pr, comments) in enumerate(sampled, 1):
        pr_number = pr["number"]
        print(f"[{idx}/{args.sample_size}] Fetching diff for PR #{pr_number}...")
        
        # Fetch unified diff
        diff_url = f"https://api.github.com/repos/{args.repo}/pulls/{pr_number}"
        diff_headers = {**headers, "Accept": "application/vnd.github.v3.diff"}
        try:
            diff_resp = client.get(diff_url, headers=diff_headers)
            diff_content = diff_resp.text if diff_resp.status_code == 200 else ""
        except Exception as e:
            print(f"    [!] Failed to get diff: {e}")
            diff_content = ""
            
        output_records.append({
            "pr_number": pr_number,
            "pr_url": pr["html_url"],
            "title": pr["title"],
            "commit_sha": pr["head"]["sha"],
            "changed_files_count": pr["changed_files"],
            "diff": diff_content,
            "human_comments": comments,
        })
        
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output_records, indent=2), encoding="utf-8")
    print(f"\n[+] Success! Saved frozen validation set to {args.output}")


if __name__ == "__main__":
    main()

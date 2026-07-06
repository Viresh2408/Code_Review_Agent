import os
import sys
import json
import random
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv()

def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[!] GITHUB_TOKEN not set in environment.")
        sys.exit(1)

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Code-Review-Agent-Validation"
    }

    # Fetch comments stream (up to 4 pages = 400 comments)
    print("[*] Fetching recent PR review comments from Flask...")
    client = httpx.Client(headers=headers, timeout=20.0)
    
    pr_comments = {}
    page = 1
    total_comments_fetched = 0
    
    while total_comments_fetched < 400:
        url = f"https://api.github.com/repos/pallets/flask/pulls/comments?per_page=100&page={page}"
        resp = client.get(url)
        if resp.status_code != 200:
            print(f"[!] Failed to fetch comments: {resp.status_code} {resp.text}")
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

    print(f"[*] Found {len(pr_comments)} unique PRs with human review comments in the recent comments stream.")
    
    # Sort PR numbers descending (newest first)
    candidate_pr_nums = sorted(pr_comments.keys(), reverse=True)
    
    filtered_prs = []
    
    for pr_num in candidate_pr_nums:
        if len(filtered_prs) >= 20:
            break
            
        print(f"    Checking PR #{pr_num}...")
        # Get PR details
        pr_url = f"https://api.github.com/repos/pallets/flask/pulls/{pr_num}"
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
        files_url = f"https://api.github.com/repos/pallets/flask/pulls/{pr_num}/files"
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
            
        print(f"    [+] PR #{pr_num} matched all constraints! Title: {pr.get('title')}")
        filtered_prs.append((pr, pr_comments[pr_num]))

    print(f"\n[*] Filtered to {len(filtered_prs)} candidate PRs.")
    
    sample_size = min(len(filtered_prs), 20)
    sampled = random.sample(filtered_prs, sample_size)
    print(f"[+] Sampled {len(sampled)} PRs deterministically.")
    
    output_records = []
    for idx, (pr, comments) in enumerate(sampled, 1):
        pr_number = pr["number"]
        print(f"[{idx}/{sample_size}] Fetching diff for PR #{pr_number}...")
        
        # Fetch unified diff
        diff_url = f"https://api.github.com/repos/pallets/flask/pulls/{pr_number}"
        diff_headers = {**headers, "Accept": "application/vnd.github.v3.diff"}
        diff_resp = client.get(diff_url, headers=diff_headers)
        diff_content = diff_resp.text if diff_resp.status_code == 200 else ""
        
        output_records.append({
            "pr_number": pr_number,
            "pr_url": pr["html_url"],
            "title": pr["title"],
            "commit_sha": pr["head"]["sha"],
            "changed_files_count": pr["changed_files"],
            "diff": diff_content,
            "human_comments": comments,
        })
        
    output_path = Path("validation/frozen_real_prs.json")
    output_path.write_text(json.dumps(output_records, indent=2), encoding="utf-8")
    print(f"\n[+] Success! Saved frozen validation set to {output_path}")

if __name__ == "__main__":
    main()

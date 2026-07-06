import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from github import Github

load_dotenv()

def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN not set!")
        return

    g = Github(token)
    repo = g.get_repo("pallets/flask")
    print("Fetching pull requests...")
    pulls = repo.get_pulls(state="closed", sort="updated", direction="desc")

    limit = 100
    count = 0
    
    passed_file_count = 0
    passed_file_type = 0
    passed_has_comments = 0
    passed_all = 0

    for pr in pulls:
        if count >= limit:
            break
        if not pr.merged:
            continue
        count += 1
        
        # Check files
        files_count = pr.changed_files
        is_file_count_ok = (1 <= files_count <= 8)
        if is_file_count_ok:
            passed_file_count += 1
            
        # Check doc/code types
        files = pr.get_files()
        is_pure_docs = True
        for f in files:
            ext = f.filename.split(".")[-1].lower() if "." in f.filename else ""
            if ext in ("py", "js", "ts", "jsx", "tsx"):
                is_pure_docs = False
                break
        
        if not is_pure_docs:
            passed_file_type += 1

        # Check human comments
        comments = pr.get_review_comments()
        human_comments = []
        for c in comments:
            user_login = c.user.login.lower()
            if "bot" in user_login or "github-actions" in user_login:
                continue
            human_comments.append(c)
        
        has_human_comments = len(human_comments) > 0
        if has_human_comments:
            passed_has_comments += 1

        if is_file_count_ok and not is_pure_docs and has_human_comments:
            passed_all += 1
            print(f"PR #{pr.number} passed all filters. Title: {pr.title}")

    print("\n--- Summary ---")
    print(f"Total merged PRs checked: {count}")
    print(f"Passed file count constraint (1-8 files): {passed_file_count}")
    print(f"Passed file type constraint (non-pure docs): {passed_file_type}")
    print(f"Has human review comments: {passed_has_comments}")
    print(f"Passed all filters: {passed_all}")

if __name__ == "__main__":
    main()

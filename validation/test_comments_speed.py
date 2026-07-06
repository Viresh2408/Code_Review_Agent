import os
from github import Github
from dotenv import load_dotenv

load_dotenv()

def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    g = Github(token)
    repo = g.get_repo("pallets/flask")
    
    print("Fetching repository pull request review comments...")
    comments = repo.get_pulls_comments()
    
    count = 0
    pr_comments = {}
    for c in comments:
        if count >= 500: # Fetch up to 500 comments
            break
        # Extract PR number from pull_request_url
        pr_url = c.pull_request_url
        pr_num = int(pr_url.split("/")[-1])
        
        if c.user is None:
            continue
        user_login = c.user.login.lower()
        if "bot" in user_login or "github-actions" in user_login:
            continue
            
        pr_comments.setdefault(pr_num, []).append(c)
        count += 1
        
    print(f"Fetched {count} comments across {len(pr_comments)} unique PRs.")
    print("Sample PR numbers:", list(pr_comments.keys())[:20])

if __name__ == "__main__":
    main()

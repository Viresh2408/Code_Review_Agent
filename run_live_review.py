import argparse
import os
import re
import sys
import time
from pathlib import Path
from github import Github

# Add paths to sys.path
root_path = Path(__file__).resolve().parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

# Load env variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# pyrefly: ignore [missing-import]
from app.parser.pipeline import ingest_pr
from agents.orchestrator import graph

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

def main():
    parser = argparse.ArgumentParser(
        description="Run the full Multi-Agent Code Review pipeline against a live GitHub PR."
    )
    parser.add_argument("pr_url", help="GitHub PR URL (e.g. https://github.com/owner/repo/pull/123)")
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Scan and review every single supported file in the entire repository at the PR's commit SHA."
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[!] Warning: GITHUB_TOKEN environment variable is not set. API calls might hit limits.")

    parsed = parse_pr_url(args.pr_url)
    if not parsed:
        print(f"[!] Error: Invalid PR URL format: {args.pr_url}")
        print("    Expected format: https://github.com/owner/repo/pull/PR_NUM")
        sys.exit(1)

    owner, repo_name, pr_number = parsed
    repo_full_name = f"{owner}/{repo_name}"

    print("=" * 72)
    print(f"  RUNNING LIVE PR REVIEW PIPELINE")
    print(f"  PR:           {args.pr_url}")
    print(f"  Repo:         {repo_full_name}")
    print(f"  PR Number:    {pr_number}")
    print("=" * 72)

    # Initialize GitHub to check commit SHA
    g = Github(token) if token else Github()
    try:
        repo = g.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        commit_sha = pr.head.sha
        print(f"[*] PR Title:    '{pr.title}' (Author: {pr.user.login})")
        print(f"[*] Head Commit:  {commit_sha[:7]} ({commit_sha})")
    except Exception as e:
        print(f"[!] Failed to fetch PR info from GitHub: {e}")
        sys.exit(1)

    # Step 1: Ingestion
    print("\n[1/3] Ingesting files and parsing ASTs from GitHub...")
    t0 = time.perf_counter()
    try:
        pr_context = ingest_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
            github_token=token,
            full_scan=args.full_scan,
        )
        print(f"  [+] Ingested {len(pr_context.changed_files)} files.")
        for f in pr_context.changed_files:
            print(f"       • {f.path} ({f.language}, {len(f.diff_hunks)} hunks)")
    except Exception as e:
        print(f"[!] Ingestion failed: {e}")
        sys.exit(1)

    # Step 2: Review Pipeline
    print("\n[2/3] Executing Multi-Agent LangGraph review DAG...")
    cost_tracker = {"total": 0.0}
    
    # Hook into log_llm_usage to track cost
    import agents.orchestrator as orch
    original_log = orch.log_llm_usage

    def tracking_log(provider, model, prompt_tokens, completion_tokens, *args, **kwargs):
        cost = original_log(provider, model, prompt_tokens, completion_tokens, *args, **kwargs)
        cost_tracker["total"] += cost
        return cost

    orch.log_llm_usage = tracking_log

    try:
        result_state = graph.invoke(pr_context)
        findings = (
            result_state.findings
            if hasattr(result_state, "findings")
            else result_state.get("findings", [])
        )
    except Exception as e:
        print(f"[!] Graph execution failed: {e}")
        sys.exit(1)
    finally:
        orch.log_llm_usage = original_log

    latency = time.perf_counter() - t0

    # Step 3: Print Findings
    print("\n[3/3] Analysis complete. Review results:")
    
    blockers = [f for f in findings if (f.severity if hasattr(f, "severity") else f.get("severity")) == "blocker"]
    warnings = [f for f in findings if (f.severity if hasattr(f, "severity") else f.get("severity")) == "warning"]
    nits     = [f for f in findings if (f.severity if hasattr(f, "severity") else f.get("severity")) == "nit"]

    print("=" * 72)
    print("  CODE REVIEW PIPELINE RESULTS")
    print("=" * 72)
    print(f"  Latency:      {latency:.2f}s")
    print(f"  Est. cost:    ${cost_tracker['total']:.4f} USD")
    print(f"  Findings:     {len(findings)} total  ({len(blockers)} blockers, {len(warnings)} warnings, {len(nits)} nits)")
    print("=" * 72)

    # Print high-level summary if available
    summary = None
    if hasattr(result_state, "pr_summary"):
        summary = result_state.pr_summary
    elif isinstance(result_state, dict) and "pr_summary" in result_state:
        summary = result_state["pr_summary"]

    if summary:
        print("\n  HIGH-LEVEL SUMMARY:")
        print(summary)
        print("=" * 72)

    if not findings:
        print("\n  No findings — clean PR! ✅")
        return

    SEVERITY_ICONS = {"blocker": "[BLOCKER]", "warning": "[WARNING]", "nit": "[NIT]    "}
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

    print("\n" + "=" * 72 + "\n")

if __name__ == "__main__":
    main()

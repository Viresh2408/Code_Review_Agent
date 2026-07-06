#!/usr/bin/env python3
"""
training/cost_comparison_report.py — Concrete cost comparison across backends.

Runs the same set of real PRs through the full review pipeline with:
  - MODEL_BACKEND=groq   (Phase 1-5 baseline)
  - MODEL_BACKEND=vllm   (self-hosted fine-tuned model)

Records per-PR and aggregate: token counts, estimated cost, wall-clock latency,
finding count. Does NOT fabricate numbers — only measured values go in the table.

Usage:
    # From a file listing PR URLs (one per line):
    python training/cost_comparison_report.py --prs-file training/comparison_prs.txt

    # Or directly:
    python training/cost_comparison_report.py --prs https://github.com/owner/repo/pull/1

    # Dry-run (no real API calls, for CI):
    python training/cost_comparison_report.py --dry-run

Requirements:
    GITHUB_TOKEN, GROQ_API_KEY, ANTHROPIC_API_KEY in environment.
    For --prs mode: vLLM must also be running when evaluating the vllm backend.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import structlog

root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

logger = structlog.get_logger(__name__)


# ── PR result container ───────────────────────────────────────────────────────

class PRResult:
    def __init__(self, pr_url: str, backend: str):
        self.pr_url = pr_url
        self.backend = backend
        self.latency_s: float = 0.0
        self.finding_count: int = 0
        self.blocker_count: int = 0
        self.warning_count: int = 0
        self.nit_count: int = 0
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.estimated_cost_usd: float = 0.0
        self.error: str | None = None


# ── Cost calculation ──────────────────────────────────────────────────────────

def estimate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int, gpu_rate: float) -> float:
    if provider == "anthropic":
        if "haiku" in model.lower():
            return (prompt_tokens * 0.80 + completion_tokens * 4.00) / 1_000_000
        return (prompt_tokens * 3.00 + completion_tokens * 15.00) / 1_000_000
    if provider == "groq":
        return (prompt_tokens * 0.59 + completion_tokens * 0.79) / 1_000_000
    if provider == "vllm":
        return (prompt_tokens + completion_tokens) * gpu_rate
    return 0.0


# ── Pipeline runner ───────────────────────────────────────────────────────────

def run_pr_with_backend(pr_url: str, backend: str, dry_run: bool) -> PRResult:
    result = PRResult(pr_url, backend)

    if dry_run:
        # Deterministic mock for CI
        import hashlib
        seed = int(hashlib.md5(pr_url.encode()).hexdigest()[:4], 16)
        result.latency_s = 2.0 + (seed % 5)
        result.finding_count = 3 + (seed % 4)
        result.blocker_count = seed % 2
        result.warning_count = 2
        result.nit_count = result.finding_count - result.blocker_count - result.warning_count
        if backend == "groq":
            result.total_prompt_tokens = 4000 + seed * 10
            result.total_completion_tokens = 800 + seed * 3
            result.estimated_cost_usd = estimate_cost("groq", "llama-3.3-70b-versatile",
                                                       result.total_prompt_tokens, result.total_completion_tokens, 0.0)
        else:
            # vLLM handles ~84% of calls, Claude ~16%
            vllm_p = int((4000 + seed * 10) * 0.84)
            vllm_c = int((800 + seed * 3) * 0.84)
            claude_p = int((4000 + seed * 10) * 0.16 * 1.1)
            claude_c = int((800 + seed * 3) * 0.16)
            result.total_prompt_tokens = vllm_p + claude_p
            result.total_completion_tokens = vllm_c + claude_c
            from app.config import get_settings
            settings = get_settings()
            result.estimated_cost_usd = (
                estimate_cost("vllm", "qwen", vllm_p, vllm_c, settings.vllm_gpu_cost_per_token)
                + estimate_cost("anthropic", "claude-sonnet-4-6", claude_p, claude_c, 0.0)
            )
        return result

    # Real run
    import re
    from dotenv import load_dotenv
    load_dotenv()

    url_pattern = r"github\.com/([^/]+/[^/]+)/pull/(\d+)"
    match = re.search(url_pattern, pr_url)
    if not match:
        result.error = f"Invalid PR URL: {pr_url}"
        return result

    repo_full_name = match.group(1)
    pr_number = int(match.group(2))

    # Temporarily set MODEL_BACKEND environment variable
    os.environ["MODEL_BACKEND"] = backend
    # Invalidate settings cache so the new env var is picked up
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    try:
        from github import Github
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            result.error = "GITHUB_TOKEN not set"
            return result
        g = Github(token)
        repo = g.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        commit_sha = pr.head.sha

        from app.parser.pipeline import ingest_pr
        pr_context = ingest_pr(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
            github_token=token,
        )

        # Patch prometheus counters to collect token/cost data during this run
        import agents.orchestrator as orch
        call_log: list[dict[str, Any]] = []
        original_log = orch.log_llm_usage

        def patched_log(provider, model, prompt_tokens, completion_tokens):
            cost = original_log(provider, model, prompt_tokens, completion_tokens)
            call_log.append({
                "provider": provider,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost": cost,
            })
            return cost

        orch.log_llm_usage = patched_log  # type: ignore[assignment]

        t0 = time.perf_counter()
        from agents.orchestrator import graph
        result_state = graph.invoke(pr_context)
        result.latency_s = time.perf_counter() - t0

        orch.log_llm_usage = original_log  # restore

        findings = (
            result_state.findings
            if hasattr(result_state, "findings")
            else result_state.get("findings", [])
        )
        result.finding_count = len(findings)
        result.blocker_count = sum(1 for f in findings if f.severity == "blocker")
        result.warning_count = sum(1 for f in findings if f.severity == "warning")
        result.nit_count = sum(1 for f in findings if f.severity == "nit")
        result.total_prompt_tokens = sum(c["prompt_tokens"] for c in call_log)
        result.total_completion_tokens = sum(c["completion_tokens"] for c in call_log)
        result.estimated_cost_usd = sum(c["cost"] for c in call_log)

    except Exception as exc:
        result.error = str(exc)
        logger.error("pr_run_failed", pr_url=pr_url, backend=backend, error=str(exc))
    finally:
        # Always restore MODEL_BACKEND to default
        os.environ.pop("MODEL_BACKEND", None)
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]

    return result


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(groq_results: list[PRResult], vllm_results: list[PRResult], dry_run: bool) -> str:
    mode_note = " *(dry-run / simulated values)*" if dry_run else ""

    def total(results: list[PRResult], field: str) -> Any:
        return sum(getattr(r, field) for r in results if r.error is None)

    def avg(results: list[PRResult], field: str) -> float:
        valid = [getattr(r, field) for r in results if r.error is None]
        return sum(valid) / len(valid) if valid else 0.0

    g_cost = total(groq_results, "estimated_cost_usd")
    v_cost = total(vllm_results, "estimated_cost_usd")
    savings = g_cost - v_cost
    savings_pct = (savings / g_cost * 100) if g_cost > 0 else 0.0

    per_pr_table = "| PR URL | Groq Cost | vLLM Cost | Groq Latency | vLLM Latency | Groq Findings | vLLM Findings |\n"
    per_pr_table += "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |\n"
    for g, v in zip(groq_results, vllm_results):
        g_err = f"ERROR: {g.error}" if g.error else f"${g.estimated_cost_usd:.5f}"
        v_err = f"ERROR: {v.error}" if v.error else f"${v.estimated_cost_usd:.5f}"
        per_pr_table += (
            f"| {g.pr_url} | {g_err} | {v_err} | "
            f"{g.latency_s:.1f}s | {v.latency_s:.1f}s | "
            f"{g.finding_count} | {v.finding_count} |\n"
        )

    return f"""# Phase 6 — Backend Cost Comparison Report{mode_note}

> [!IMPORTANT]
> These numbers are **measured from real PR runs**, not estimated.
> Do not quote cost percentages that were not produced by this script.

## Aggregate Summary

| Metric | Groq Baseline | vLLM Hybrid | Delta / Savings |
| :--- | :---: | :---: | :---: |
| **Total Prompt Tokens** | {total(groq_results, 'total_prompt_tokens'):,} | {total(vllm_results, 'total_prompt_tokens'):,} | {total(vllm_results, 'total_prompt_tokens') - total(groq_results, 'total_prompt_tokens'):+,} |
| **Total Completion Tokens** | {total(groq_results, 'total_completion_tokens'):,} | {total(vllm_results, 'total_completion_tokens'):,} | {total(vllm_results, 'total_completion_tokens') - total(groq_results, 'total_completion_tokens'):+,} |
| **Avg Latency per PR** | {avg(groq_results, 'latency_s'):.1f}s | {avg(vllm_results, 'latency_s'):.1f}s | {avg(vllm_results, 'latency_s') - avg(groq_results, 'latency_s'):+.1f}s |
| **Total Cost** | **${g_cost:.4f}** | **${v_cost:.4f}** | **${savings:.4f} ({savings_pct:.1f}% savings)** |
| **Avg Cost per PR** | ${avg(groq_results, 'estimated_cost_usd'):.4f} | ${avg(vllm_results, 'estimated_cost_usd'):.4f} | ${avg(vllm_results, 'estimated_cost_usd') - avg(groq_results, 'estimated_cost_usd'):+.4f} |

> **Note on vLLM cost:** modeled as amortized GPU cost per token (see `VLLM_GPU_COST_PER_TOKEN` in config).
> The $0.00 API cost of self-hosting is not used — that would misrepresent the true cost comparison.

## Per-PR Breakdown

{per_pr_table}

## PRs Evaluated

{chr(10).join(f"- {r.pr_url}" for r in groq_results)}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real PRs through both backends and produce a cost comparison.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prs", nargs="+", metavar="URL", help="One or more GitHub PR URLs.")
    group.add_argument(
        "--prs-file",
        type=Path,
        default=root_path / "training" / "comparison_prs.txt",
        help="File with one PR URL per line.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root_path / "docs" / "cost_comparison_report.md",
        help="Output markdown report path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use mocked data instead of real API calls (for CI / local testing).",
    )
    args = parser.parse_args()

    # Resolve PR list
    if args.prs:
        pr_urls = args.prs
    elif args.prs_file.exists():
        pr_urls = [line.strip() for line in args.prs_file.read_text().splitlines() if line.strip()]
    elif args.dry_run:
        # Synthetic PRs for dry run
        pr_urls = [
            "https://github.com/example/repo/pull/1",
            "https://github.com/example/repo/pull/2",
            "https://github.com/example/repo/pull/3",
        ]
    else:
        print("[!] No PR URLs provided. Use --prs <url...> or --prs-file <path> or --dry-run.")
        sys.exit(1)

    print("=" * 80)
    print(" PHASE 6 — BACKEND COST COMPARISON REPORT")
    print("=" * 80)
    if args.dry_run:
        print("[*] DRY RUN MODE — using simulated data.")
    print(f"[*] Evaluating {len(pr_urls)} PRs across 2 backends (groq, vllm)...")

    groq_results: list[PRResult] = []
    vllm_results: list[PRResult] = []

    for i, pr_url in enumerate(pr_urls, 1):
        print(f"\n[{i}/{len(pr_urls)}] PR: {pr_url}")
        for backend in ("groq", "vllm"):
            print(f"  → Running with backend={backend}...", end=" ", flush=True)
            result = run_pr_with_backend(pr_url, backend, dry_run=args.dry_run)
            if result.error:
                print(f"ERROR: {result.error}")
            else:
                print(f"done ({result.latency_s:.1f}s, ${result.estimated_cost_usd:.5f}, {result.finding_count} findings)")
            if backend == "groq":
                groq_results.append(result)
            else:
                vllm_results.append(result)
        if not args.dry_run:
            time.sleep(2)  # avoid hammering APIs between PRs

    report = build_report(groq_results, vllm_results, dry_run=args.dry_run)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")

    g_total = sum(r.estimated_cost_usd for r in groq_results if not r.error)
    v_total = sum(r.estimated_cost_usd for r in vllm_results if not r.error)
    savings = g_total - v_total
    savings_pct = (savings / g_total * 100) if g_total > 0 else 0.0

    print("\n" + "=" * 60)
    print(f"  Groq total cost:  ${g_total:.4f}")
    print(f"  vLLM total cost:  ${v_total:.4f}")
    print(f"  Savings:          ${savings:.4f} ({savings_pct:.1f}%)")
    print("=" * 60)
    print(f"\n[+] Report saved to {args.output}")


if __name__ == "__main__":
    main()

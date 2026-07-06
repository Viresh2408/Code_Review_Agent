#!/usr/bin/env python3
"""
validation/run_real_pr_eval.py

Runs the validation PR set through the Code Review Agent pipeline and compares
findings against human comments to evaluate precision, recall, and F1 metrics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add project paths to sys.path
root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

from app.config import get_settings
from app.parser.pipeline import ingest_pr
from agents.orchestrator import graph
from unittest.mock import patch
from dotenv import load_dotenv

load_dotenv()


class EvalStats:
    def __init__(self):
        self.tp = 0
        self.fp = 0
        self.fn = 0

    def precision(self) -> float:
        total_pred = self.tp + self.fp
        return self.tp / total_pred if total_pred > 0 else 1.0

    def recall(self) -> float:
        total_actual = self.tp + self.fn
        return self.tp / total_actual if total_actual > 0 else 1.0

    def f1(self) -> float:
        p = self.precision()
        r = self.recall()
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def match_findings(agent_findings: list, human_comments: list) -> tuple[int, int, int, list[dict]]:
    """
    Match agent findings to human comments.
    Match condition: same file AND line difference <= 3 lines.
    Returns (tp, fp, fn, matched_details).
    """
    tp = 0
    matched_humans = set()
    matched_agents = set()
    matched_details = []

    for a_idx, af in enumerate(agent_findings):
        a_file = af.get("file_path", "")
        a_line = af.get("line") or af.get("line_number")
        
        # Check against all human comments
        for h_idx, hc in enumerate(human_comments):
            if h_idx in matched_humans:
                continue
                
            h_file = hc.get("file_path", "")
            h_line = hc.get("line")

            # Match criteria
            files_match = a_file.lower().endswith(h_file.lower()) or h_file.lower().endswith(a_file.lower())
            lines_close = (
                a_line is not None 
                and h_line is not None 
                and abs(a_line - h_line) <= 3
            )

            if files_match and lines_close:
                tp += 1
                matched_humans.add(h_idx)
                matched_agents.add(a_idx)
                matched_details.append({
                    "agent_finding": af,
                    "human_comment": hc,
                })
                break

    fp = len(agent_findings) - len(matched_agents)
    fn = len(human_comments) - len(matched_humans)

    return tp, fp, fn, matched_details


def run_mock_pipeline(pr_context: Any) -> Any:
    """Mock review execution to avoid API calls and costs during dry runs/CI."""
    from agents.schemas import Finding
    
    findings = []
    # Deterministically generate a mock finding to match at least one comment if possible
    # to test stats calculations
    for file in pr_context.changed_files:
        findings.append(
            Finding(
                agent="security_agent",
                file_path=file.path,
                line=5,
                severity="warning",
                category="security",
                message="Mock validation finding",
                confidence=0.85,
                suggested_fix=None,
                escalated_to_claude=False,
            )
        )
    
    # Pack findings back into state
    from pydantic import BaseModel
    class MockState(BaseModel):
        findings: list = []
        
    return MockState(findings=findings)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate agent code reviews against human ground truth.")
    parser.add_argument(
        "--input",
        type=Path,
        default=root_path / "validation" / "frozen_real_prs.json",
        help="Path to frozen real PRs dataset.",
    )
    parser.add_argument(
        "--output-report",
        type=Path,
        default=root_path / "docs" / "real_pr_precision_recall.md",
        help="Path to save evaluation report.",
    )
    parser.add_argument(
        "--repo",
        default="pallets/flask",
        help="Repository full name used to match files.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run using simulated agent outputs instead of calling live LLM APIs.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"[!] Input file not found: {args.input}")
        print("    Run validation/select_real_prs.py first to sample PRs.")
        sys.exit(1)

    with open(args.input, "r", encoding="utf-8") as f:
        prs_data = json.load(f)

    print(f"[*] Loaded {len(prs_data)} PRs for evaluation.")
    
    # Global stats
    global_stats = EvalStats()
    # Stats per agent category
    agent_stats = {
        "security_agent": EvalStats(),
        "architecture_agent": EvalStats(),
        "test_coverage_agent": EvalStats(),
    }
    # Stats per severity
    severity_stats = {
        "blocker": EvalStats(),
        "warning": EvalStats(),
        "nit": EvalStats(),
    }

    detailed_results = []

    # Mock Neo4j to avoid connections and retries
    with (
        patch("app.parser.pipeline.ingest_file_to_neo4j"),
        patch("app.parser.pipeline.get_changed_functions", return_value=[]),
        patch("app.parser.pipeline.get_blast_radius", return_value=[])
    ):
        for idx, item in enumerate(prs_data, 1):
            pr_num = item["pr_number"]
            sha = item["commit_sha"]
            human_comments = item["human_comments"]
            
            print(f"\n[{idx}/{len(prs_data)}] Evaluating PR #{pr_num} (SHA: {sha[:7]})...")
            
            try:
                # 1. Ingest PR content from GitHub
                pr_context = ingest_pr(
                    repo_full_name=args.repo,
                    pr_number=pr_num,
                    commit_sha=sha,
                )
                
                # 2. Run reviews (Live vs Mock)
                if args.mock:
                    res_state = run_mock_pipeline(pr_context)
                else:
                    res_state = graph.invoke(pr_context)
                    
                agent_findings_raw = getattr(res_state, "findings", [])
                
                # Map to standard dictionaries
                agent_findings = []
                for f in agent_findings_raw:
                    if hasattr(f, "model_dump"):
                        agent_findings.append(f.model_dump())
                    elif isinstance(f, dict):
                        agent_findings.append(f)
                    else:
                        agent_findings.append({
                            "agent": getattr(f, "agent", "unknown"),
                            "file_path": getattr(f, "file_path", ""),
                            "line": getattr(f, "line", 0),
                            "severity": getattr(f, "severity", "warning"),
                            "category": getattr(f, "category", ""),
                            "message": getattr(f, "message", ""),
                        })

                # 3. Match agent findings against human comments
                tp, fp, fn, matched = match_findings(agent_findings, human_comments)
                
                print(f"    -> Agent Findings: {len(agent_findings)} | Human Comments: {len(human_comments)}")
                print(f"    -> TP: {tp} | FP: {fp} | FN: {fn}")
                
                # Update global stats
                global_stats.tp += tp
                global_stats.fp += fp
                global_stats.fn += fn
                
                # Distribute stats by agent category and severity
                # Note: to do this accurately we match subsets
                for f in agent_findings:
                    f_agent = f.get("agent", "security_agent")
                    f_sev = f.get("severity", "warning")
                    
                    _, item_fp, _, _ = match_findings([f], human_comments)
                    if item_fp == 0:
                        # Matched!
                        if f_agent in agent_stats:
                            agent_stats[f_agent].tp += 1
                        if f_sev in severity_stats:
                            severity_stats[f_sev].tp += 1
                    else:
                        # Unmatched (False Positive)
                        if f_agent in agent_stats:
                            agent_stats[f_agent].fp += 1
                        if f_sev in severity_stats:
                            severity_stats[f_sev].fp += 1

                for h in human_comments:
                    _, _, h_fn, _ = match_findings(agent_findings, [h])
                    if h_fn > 0:
                        # Unmatched human comment (False Negative)
                        # Distribute to respective target agents based on comment content heuristic
                        h_body = h.get("body", "").lower()
                        target_agent = "architecture_agent"
                        if any(x in h_body for x in ("security", "vuln", "inject", "crypt", "secret", "password")):
                            target_agent = "security_agent"
                        elif any(x in h_body for x in ("test", "coverage", "assert", "mock")):
                            target_agent = "test_coverage_agent"
                            
                        agent_stats[target_agent].fn += 1
                        severity_stats["warning"].fn += 1  # default mapping

                detailed_results.append({
                    "pr_number": pr_num,
                    "pr_url": item["pr_url"],
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "findings_count": len(agent_findings),
                    "human_comments_count": len(human_comments),
                    "matched": matched,
                })
                
                if not args.mock:
                    time.sleep(1.0) # Courtesy pause

            except Exception as e:
                print(f"    [!] Error processing PR #{pr_num}: {e}")
                import traceback
                traceback.print_exc()

    # Write report
    report_md = f"""# Real-World PR Selection & Evaluation Report
{"*(Simulated evaluation run)*" if args.mock else ""}

This report compares findings from the Code Review Agent against ground-truth comments written by human reviewers on real pull requests.

## Aggregate Accuracy Metrics

| Metric | Score | Formula / Details |
| :--- | :---: | :--- |
| **Precision** | **{global_stats.precision():.2%}** | True Positives / (True Positives + False Positives) |
| **Recall** | **{global_stats.recall():.2%}** | True Positives / (True Positives + False Negatives) |
| **F1-Score** | **{global_stats.f1():.2%}** | Harmonic mean of Precision and Recall |
| True Positives | {global_stats.tp} | Agent findings matching human comments (line ±3) |
| False Positives | {global_stats.fp} | Agent findings without matching human comments |
| False Negatives | {global_stats.fn} | Human comments missed by the agent |

> **Honesty Caveat:** Detections flagged as "False Positives" (unmatched agent findings) represent code blocks flagged by the agent where the human reviewer did not leave a comment. This includes cases where the human reviewer missed a genuine vulnerability or architectural issue. 

---

## Performance Breakdown

### By Agent
| Agent | Precision | Recall | F1 | TP / FP / FN |
| :--- | :---: | :---: | :---: | :---: |
| **Security Agent** | {agent_stats['security_agent'].precision():.1%} | {agent_stats['security_agent'].recall():.1%} | {agent_stats['security_agent'].f1():.1%} | {agent_stats['security_agent'].tp}/{agent_stats['security_agent'].fp}/{agent_stats['security_agent'].fn} |
| **Architecture Agent** | {agent_stats['architecture_agent'].precision():.1%} | {agent_stats['architecture_agent'].recall():.1%} | {agent_stats['architecture_agent'].f1():.1%} | {agent_stats['architecture_agent'].tp}/{agent_stats['architecture_agent'].fp}/{agent_stats['architecture_agent'].fn} |
| **Test-Coverage Agent** | {agent_stats['test_coverage_agent'].precision():.1%} | {agent_stats['test_coverage_agent'].recall():.1%} | {agent_stats['test_coverage_agent'].f1():.1%} | {agent_stats['test_coverage_agent'].tp}/{agent_stats['test_coverage_agent'].fp}/{agent_stats['test_coverage_agent'].fn} |

### By Severity
| Severity | Precision | Recall | F1 | TP / FP / FN |
| :--- | :---: | :---: | :---: | :---: |
| **Blocker** | {severity_stats['blocker'].precision():.1%} | {severity_stats['blocker'].recall():.1%} | {severity_stats['blocker'].f1():.1%} | {severity_stats['blocker'].tp}/{severity_stats['blocker'].fp}/{severity_stats['blocker'].fn} |
| **Warning** | {severity_stats['warning'].precision():.1%} | {severity_stats['warning'].recall():.1%} | {severity_stats['warning'].f1():.1%} | {severity_stats['warning'].tp}/{severity_stats['warning'].fp}/{severity_stats['warning'].fn} |
| **Nit** | {severity_stats['nit'].precision():.1%} | {severity_stats['nit'].recall():.1%} | {severity_stats['nit'].f1():.1%} | {severity_stats['nit'].tp}/{severity_stats['nit'].fp}/{severity_stats['nit'].fn} |

---

## Per-PR Evaluation Details

| PR Link | Agent Findings | Human Comments | TP | FP | FN |
| :--- | :---: | :---: | :---: | :---: | :---: |
"""

    for r in detailed_results:
        report_md += f"| [{r['pr_number']}]({r['pr_url']}) | {r['findings_count']} | {r['human_comments_count']} | {r['tp']} | {r['fp']} | {r['fn']} |\n"

    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    args.output_report.write_text(report_md, encoding="utf-8")
    print(f"\n[+] Success! Report written to {args.output_report}")


if __name__ == "__main__":
    main()

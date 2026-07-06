#!/usr/bin/env python3
"""
training/export_findings.py — Rigorous training data exporter.

Closes the gaps in backend/scripts/export_training_data.py:
  - Deterministic ordering (ORDER BY finding.id ASC) for idempotent output.
  - Correct labeling: confirmed escalations → positive, rejected → negative.
  - Volume gate: warns and exits if own-data count < 200 (use --force to bypass).
  - `source` field per JSONL line for later ablation.
  - SHA-256 manifest written after export so train_lora.py can log the exact
    dataset version that produced each MLflow run.

Usage:
    python training/export_findings.py [--output PATH] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

# ── Path setup ────────────────────────────────────────────────────────────────
root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker, joinedload

from app.config import get_settings
from app.models import Finding, Review, PullRequest
from agents.orchestrator import (
    SECURITY_PROMPT,
    ARCHITECTURE_PROMPT,
    TEST_COVERAGE_PROMPT,
)

logger = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MINIMUM_OWN_DATA_EXAMPLES = 200

# Offline public fallback examples (same as backend/scripts/export_training_data.py
# but with the source field and richer labeling documentation).
OFFLINE_PUBLIC_SAMPLES = [
    {
        "agent": "security_agent",
        "diff_hunk": (
            "@@ -15,5 +15,7 @@\n def login(username, password):\n"
            "+    query = f\"SELECT * FROM users WHERE user='{username}' AND pwd='{password}'\"\n"
            "+    cursor.execute(query)\n     return True"
        ),
        "ast_summary": "def login(username, password)",
        "blast_radius": ["authenticate_user (auth.py)"],
        "repo_conventions": "Use parameterized SQL queries.",
        "findings": [
            {
                "line": 16,
                "severity": "blocker",
                "message": "SQL Injection risk: raw string formatting used in SQL query.",
                "confidence": 0.95,
                "suggested_fix": 'cursor.execute("SELECT * FROM users WHERE user=%s AND pwd=%s", (username, password))',
            }
        ],
    },
    {
        "agent": "security_agent",
        "diff_hunk": '@@ -1,3 +1,4 @@\n-API_KEY = None\n+API_KEY = "sk-live-5e8aef12bc519de104a"\n+def get_auth_headers():',
        "ast_summary": "API_KEY = ...",
        "blast_radius": [],
        "repo_conventions": "Never commit credentials or API keys to the repository.",
        "findings": [
            {
                "line": 2,
                "severity": "blocker",
                "message": "Hardcoded API secret key detected.",
                "confidence": 0.99,
                "suggested_fix": "API_KEY = os.environ.get('API_KEY')",
            }
        ],
    },
    {
        "agent": "architecture_agent",
        "diff_hunk": (
            "@@ -10,4 +10,6 @@\n def calculate_price(item_id):\n"
            '+    import os\n+    print(f"Fetching price for {item_id}")\n+    return db.query_price(item_id)'
        ),
        "ast_summary": "def calculate_price(item_id)",
        "blast_radius": ["checkout (orders.py)"],
        "repo_conventions": "Use structured logger wrapper. Do not use print.",
        "findings": [
            {
                "line": 12,
                "severity": "nit",
                "message": "Use of print statement violates logging conventions.",
                "confidence": 0.88,
                "suggested_fix": "logger.info('fetching_price', item_id=item_id)",
            }
        ],
    },
    # A clean example (no findings) — important for FP-rate calibration.
    {
        "agent": "security_agent",
        "diff_hunk": (
            "@@ -5,4 +5,6 @@\n def load_config(path: str) -> dict:\n"
            "+    with open(path, 'rb') as f:\n+        return json.load(f)\n     return {}"
        ),
        "ast_summary": "def load_config(path: str) -> dict",
        "blast_radius": ["app_startup (main.py)"],
        "repo_conventions": "Config files must be loaded via the config module.",
        "findings": [],  # ← clean example, correct output is {"findings": []}
    },
    {
        "agent": "test_coverage_agent",
        "diff_hunk": (
            "@@ -50,6 +50,12 @@\n def new_billing_flow(user_id):\n"
            '+    if not user_id:\n+        raise ValueError("Empty user id")\n+    process_payment(user_id)\n     return True'
        ),
        "test_diff_hunks": "No test files changed in this PR.",
        "findings": [
            {
                "line": 52,
                "severity": "warning",
                "message": "New branch raising ValueError lacks corresponding unit test coverage.",
                "confidence": 0.90,
                "suggested_fix": None,
            }
        ],
    },
]


# ── Core logic ────────────────────────────────────────────────────────────────

def build_record(agent: str, diff_hunk: str, ast_summary: str,
                 blast_radius: str, repo_conventions: str,
                 test_diff_hunks: str, findings: list[dict],
                 source: str) -> dict:
    """Format one JSONL training record."""
    output_json = json.dumps({"findings": findings})

    if agent == "security_agent":
        inst = SECURITY_PROMPT.split("--- DIFF HUNK ---")[0].strip()
        inp = (
            f"--- DIFF HUNK ---\n{diff_hunk}\n\n"
            f"--- AST SUMMARY ---\n{ast_summary}\n\n"
            f"--- BLAST RADIUS ---\n{blast_radius}\n\n"
            f"--- REPO CONVENTIONS ---\n{repo_conventions}"
        )
    elif agent == "architecture_agent":
        inst = ARCHITECTURE_PROMPT.split("--- DIFF HUNK ---")[0].strip()
        inp = (
            f"--- DIFF HUNK ---\n{diff_hunk}\n\n"
            f"--- BLAST RADIUS ---\n{blast_radius}\n\n"
            f"--- REPO CONVENTIONS ---\n{repo_conventions}"
        )
    elif agent == "test_coverage_agent":
        inst = TEST_COVERAGE_PROMPT.split("--- DIFF HUNK (source) ---")[0].strip()
        inp = (
            f"--- DIFF HUNK (source) ---\n{diff_hunk}\n\n"
            f"--- DIFF HUNK (tests changed in this PR, if any) ---\n{test_diff_hunks}"
        )
    else:
        return {}  # debt_scoring agent: skip, different format

    return {"instruction": inst, "input": inp, "output": output_json, "source": source}


def generate_synthetic_context(finding: Finding) -> tuple[str, str, str, str]:
    """Reconstruct plausible diff context from a DB finding (same heuristics as the old script)."""
    diff_hunk = "@@ -10,5 +10,10 @@\n# Modified line\n+" + finding.message
    ast_summary = f"def modified_function_{finding.id}()"
    blast_radius = "None"
    repo_conventions = "Avoid security vulnerabilities and keep architectural consistency."

    if finding.agent == "security_agent":
        if "sql" in finding.message.lower():
            diff_hunk = (
                "@@ -5,5 +5,7 @@\n def query_db(data):\n"
                "+    query = f\"SELECT * FROM data WHERE val = '{data}'\"\n+    db.execute(query)"
            )
            ast_summary = "def query_db(data)"
            repo_conventions = "Use parameterized SQL queries."
        elif any(kw in finding.message.lower() for kw in ("secret", "key", "credential")):
            diff_hunk = '@@ -1,2 +1,3 @@\n+SECRET_KEY = "amFzb25fa2V5X3NlY3JldF8xMjM0"\n def get_secret():'
            ast_summary = "SECRET_KEY = ..."
            repo_conventions = "Secrets should be read from environment."
    elif finding.agent == "architecture_agent":
        if any(kw in finding.message.lower() for kw in ("logging", "print")):
            diff_hunk = "@@ -12,4 +12,5 @@\n def process_job():\n+    print('Job started')\n     run_job()"
            ast_summary = "def process_job()"
            repo_conventions = "Use structured logger wrapper. Do not use print."
    elif finding.agent == "test_coverage_agent":
        diff_hunk = (
            "@@ -25,5 +25,8 @@\n def handle_request(req):\n"
            "+    if not req.is_valid():\n+        return {'error': 'Invalid request'}\n     return process(req)"
        )
        ast_summary = "def handle_request(req)"

    return diff_hunk, ast_summary, blast_radius, repo_conventions


def export_own_findings(session) -> list[dict]:
    """
    Export labeled training examples from the database.

    Labeling rules (documented in implementation_plan.md §Q2):
      - escalated_to_claude=False                  → positive example (model accepted with high confidence)
      - escalated_to_claude=True, outcome=confirmed → positive example (Claude confirmed it)
      - escalated_to_claude=True, outcome=rejected  → negative example (output should be {"findings": []})
      - escalated_to_claude=True, outcome=n/a       → skipped (outcome unknown, don't add noise)

    Ordered by finding.id ASC for deterministic, idempotent output.
    """
    findings = (
        session.execute(
            select(Finding)
            .options(joinedload(Finding.review))
            .order_by(Finding.id.asc())
        )
        .scalars()
        .all()
    )

    records: list[dict] = []
    for f in findings:
        # Skip unknown escalation outcomes to avoid noisy data
        if f.escalated_to_claude and f.escalation_outcome == "n/a":
            logger.debug("skipping_finding_unknown_escalation_outcome", finding_id=f.id)
            continue

        diff_hunk, ast_summary, blast_radius, repo_conventions = generate_synthetic_context(f)
        test_diff_hunks = "No test files changed in this PR."

        # Determine label
        if f.escalated_to_claude and f.escalation_outcome == "rejected":
            # Claude rejected this finding → teach the model to return no findings
            output_findings: list[dict] = []
        else:
            # Positive example
            output_findings = [
                {
                    "line": f.line_number,
                    "severity": f.severity,
                    "message": f.message,
                    "confidence": float(f.confidence or 0.9),
                    "suggested_fix": f.suggested_fix,
                }
            ]

        record = build_record(
            agent=f.agent,
            diff_hunk=diff_hunk,
            ast_summary=ast_summary,
            blast_radius=blast_radius,
            repo_conventions=repo_conventions,
            test_diff_hunks=test_diff_hunks,
            findings=output_findings,
            source="own_findings",
        )
        if record:
            records.append(record)

    return records


def export_public_fallback() -> list[dict]:
    """Convert offline public samples to training records with source='public_dataset'."""
    records: list[dict] = []
    for sample in OFFLINE_PUBLIC_SAMPLES:
        agent = sample["agent"]
        diff = sample["diff_hunk"]
        ast = sample.get("ast_summary", "")
        blast = ", ".join(sample.get("blast_radius", [])) or "None"
        convs = sample.get("repo_conventions", "")
        test_hunks = sample.get("test_diff_hunks", "No test files changed in this PR.")
        output_findings = sample["findings"]

        record = build_record(
            agent=agent,
            diff_hunk=diff,
            ast_summary=ast,
            blast_radius=blast,
            repo_conventions=convs,
            test_diff_hunks=test_hunks,
            findings=output_findings,
            source="public_dataset",
        )
        if record:
            records.append(record)
    return records


def write_manifest(output_path: Path, records: list[dict]) -> str:
    """Compute SHA-256 of the output file and write a manifest JSON alongside it."""
    content = output_path.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()

    own_count = sum(1 for r in records if r.get("source") == "own_findings")
    public_count = sum(1 for r in records if r.get("source") == "public_dataset")

    manifest = {
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "output_file": str(output_path),
        "sha256": sha256,
        "total_examples": len(records),
        "own_findings_count": own_count,
        "public_dataset_count": public_count,
        "minimum_own_required": MINIMUM_OWN_DATA_EXAMPLES,
        "volume_gate_passed": own_count >= MINIMUM_OWN_DATA_EXAMPLES,
    }

    manifest_path = output_path.parent / "dataset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return sha256


def main(output_path: Path, force: bool) -> None:
    print("[*] Starting rigorous training data export...")
    settings = get_settings()

    # ── 1. Own findings from database ──────────────────────────────────────────
    own_records: list[dict] = []
    try:
        engine = create_engine(settings.sync_database_url)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            own_records = export_own_findings(session)
        print(f"[*] Exported {len(own_records)} labeled examples from database.")
    except Exception as exc:
        print(f"[!] Database read failed: {exc}. Proceeding with public fallback only.")

    # ── 2. Public dataset supplementation ─────────────────────────────────────
    public_records: list[dict] = []
    try:
        from datasets import load_dataset  # type: ignore[import]
        dataset = load_dataset("m-a-p/CodeFeedback-Filter-v1", split="train", streaming=True)
        count = 0
        for item in dataset:
            if count >= 30:
                break
            query = item.get("query", "")
            answer = item.get("answer", "")
            if len(query) < 1000 and len(answer) < 1000:
                public_records.append({
                    "instruction": "Explain the following code snippet and provide code review suggestions.",
                    "input": query,
                    "output": answer,
                    "source": "public_dataset",
                })
                count += 1
        print(f"[*] Blended {count} HuggingFace public samples.")
    except Exception as hf_exc:
        print(f"[*] HuggingFace offline or skipped ({hf_exc}). Using local fallback samples.")
        public_records = export_public_fallback()
        print(f"[*] Blended {len(public_records)} local offline public samples.")

    all_records = own_records + public_records

    # ── 3. Volume gate ─────────────────────────────────────────────────────────
    own_count = len(own_records)
    if own_count < MINIMUM_OWN_DATA_EXAMPLES:
        msg = (
            f"\n[!] VOLUME GATE: Only {own_count} own-data examples found "
            f"(minimum {MINIMUM_OWN_DATA_EXAMPLES} required for reliable fine-tuning).\n"
            "    A model trained on this little own data will likely underfit.\n"
            "    This is expected early on — the model still uses public data, but\n"
            "    your own findings are what make it repo-specific.\n"
            "    Re-run with --force to bypass and proceed with combined dataset anyway."
        )
        print(msg)
        if not force:
            sys.exit(1)
        print("[!] --force passed. Bypassing volume gate and continuing.")

    # ── 4. Write JSONL ─────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for record in all_records:
            f.write(json.dumps(record) + "\n")

    sha256 = write_manifest(output_path, all_records)

    print(f"\n[+] Exported {len(all_records)} total records:")
    print(f"    Own findings:    {own_count}")
    print(f"    Public dataset:  {len(public_records)}")
    print(f"    Output:          {output_path}")
    print(f"    SHA-256:         {sha256}")
    print(f"    Manifest:        {output_path.parent / 'dataset_manifest.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export labeled training data from the findings database.")
    parser.add_argument(
        "--output",
        type=Path,
        default=root_path / "training" / "training_data.jsonl",
        help="Path to write the output JSONL file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the volume gate (< 200 own examples) and proceed anyway.",
    )
    args = parser.parse_args()
    main(output_path=args.output, force=args.force)

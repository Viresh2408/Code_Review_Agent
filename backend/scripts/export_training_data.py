#!/usr/bin/env python3
"""
Training Data Exporter Script.
Queries findings from the SQLite database, reconstructs diff hunks,
blends them with public dataset examples, and formats them into a valid JSONL file.
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import structlog

# Add root folder and backend folder to sys.path
root_path = Path(__file__).resolve().parents[2]
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import get_settings
from app.models import Finding, Review, PullRequest

logger = structlog.get_logger(__name__)

# Let's import prompts to match format
from agents.orchestrator import (
    SECURITY_PROMPT,
    ARCHITECTURE_PROMPT,
    TEST_COVERAGE_PROMPT,
    DEBT_SCORING_PROMPT,
)

OFFLINE_PUBLIC_SAMPLES = [
    # Security
    {
        "agent": "security_agent",
        "diff_hunk": "@@ -15,5 +15,7 @@\n def login(username, password):\n+    query = f\"SELECT * FROM users WHERE user='{username}' AND pwd='{password}'\"\n+    cursor.execute(query)\n     return True",
        "ast_summary": "def login(username, password)",
        "blast_radius": ["authenticate_user (auth.py)"],
        "repo_conventions": "Use parameterized SQL queries.",
        "findings": [
            {
                "line": 16,
                "severity": "blocker",
                "message": "SQL Injection risk detected due to raw string formatting in SQL query.",
                "confidence": 0.95,
                "suggested_fix": "cursor.execute(\"SELECT * FROM users WHERE user=%s AND pwd=%s\", (username, password))"
            }
        ]
    },
    {
        "agent": "security_agent",
        "diff_hunk": "@@ -1,3 +1,4 @@\n-API_KEY = None\n+API_KEY = \"sk-live-5e8aef12bc519de104a\"\n+def get_auth_headers():",
        "ast_summary": "API_KEY = ...",
        "blast_radius": [],
        "repo_conventions": "Never commit credentials or API keys to the repository.",
        "findings": [
            {
                "line": 2,
                "severity": "blocker",
                "message": "Hardcoded API secret key detected.",
                "confidence": 0.99,
                "suggested_fix": "API_KEY = os.environ.get('API_KEY')"
            }
        ]
    },
    # Architecture
    {
        "agent": "architecture_agent",
        "diff_hunk": "@@ -10,4 +10,6 @@\n def calculate_price(item_id):\n+    import os\n+    print(f\"Fetching price for {item_id}\")\n+    return db.query_price(item_id)",
        "ast_summary": "def calculate_price(item_id)",
        "blast_radius": ["checkout (orders.py)"],
        "repo_conventions": "Use structured logger wrapper. Do not use print.",
        "findings": [
            {
                "line": 12,
                "severity": "nit",
                "message": "Use of print statement violates logging conventions.",
                "confidence": 0.88,
                "suggested_fix": "logger.info('fetching_price', item_id=item_id)"
            }
        ]
    },
    # Test Coverage
    {
        "agent": "test_coverage_agent",
        "diff_hunk": "@@ -50,6 +50,12 @@\n def new_billing_flow(user_id):\n+    if not user_id:\n+        raise ValueError(\"Empty user id\")\n+    process_payment(user_id)\n     return True",
        "test_diff_hunks": "No test files changed in this PR.",
        "findings": [
            {
                "line": 52,
                "severity": "warning",
                "message": "New branch raising ValueError lacks corresponding unit test coverage.",
                "confidence": 0.90,
                "suggested_fix": None
            }
        ]
    }
]


def generate_synthetic_context(finding: Finding) -> tuple[str, str, str, str]:
    """Reconstruct plausible diff hunk and context based on a DB finding."""
    # Default values
    diff_hunk = "@@ -10,5 +10,10 @@\n# Modified line\n+" + finding.message
    ast_summary = f"def modified_function_{finding.id}()"
    blast_radius = "None"
    repo_conventions = "Avoid security vulnerabilities and keep architectural consistency."

    if finding.agent == "security_agent":
        if "sql" in finding.message.lower():
            diff_hunk = "@@ -5,5 +5,7 @@\n def query_db(data):\n+    query = f\"SELECT * FROM data WHERE val = '{data}'\"\n+    db.execute(query)"
            ast_summary = "def query_db(data)"
            repo_conventions = "Use parameterized SQL queries."
        elif "secret" in finding.message.lower() or "key" in finding.message.lower():
            diff_hunk = "@@ -1,2 +1,3 @@\n+SECRET_KEY = \"amFzb25fa2V5X3NlY3JldF8xMjM0\"\n def get_secret():"
            ast_summary = "SECRET_KEY = ..."
            repo_conventions = "Secrets should be read from environment."
    elif finding.agent == "architecture_agent":
        if "logging" in finding.message.lower() or "print" in finding.message.lower():
            diff_hunk = "@@ -12,4 +12,5 @@\n def process_job():\n+    print('Job started')\n     run_job()"
            ast_summary = "def process_job()"
            repo_conventions = "Use structured logger wrapper. Do not use print."
    elif finding.agent == "test_coverage_agent":
        diff_hunk = "@@ -25,5 +25,8 @@\n def handle_request(req):\n+    if not req.is_valid():\n+        return {'error': 'Invalid request'}\n     return process(req)"
        ast_summary = "def handle_request(req)"

    return diff_hunk, ast_summary, blast_radius, repo_conventions


def main() -> None:
    print("[*] Starting training data exporter...")
    settings = get_settings()
    
    # Initialize SQLAlchemy connection
    engine = create_engine(settings.sync_database_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    jsonl_records = []

    # 1. Export findings from local database
    try:
        findings = session.query(Finding).all()
        print(f"[*] Found {len(findings)} findings in local database.")
        
        # Group findings by review_id & file_path to form prompt inputs
        grouped: dict[tuple[int, str], list[Finding]] = {}
        for f in findings:
            grouped.setdefault((f.review_id, f.file_path), []).append(f)

        for (review_id, file_path), file_findings in grouped.items():
            # Get one agent type
            agent = file_findings[0].agent
            diff_hunk, ast_summary, blast_radius, repo_conventions = generate_synthetic_context(file_findings[0])

            # Prepare prompts and JSON output
            output_findings = []
            for f in file_findings:
                output_findings.append({
                    "line": f.line_number,
                    "severity": f.severity,
                    "message": f.message,
                    "confidence": float(f.confidence or 0.9),
                    "suggested_fix": f.suggested_fix
                })
            
            output_json = json.dumps({"findings": output_findings})

            if agent == "security_agent":
                prompt_inst = SECURITY_PROMPT.split("--- DIFF HUNK ---")[0].strip()
                prompt_input = f"--- DIFF HUNK ---\n{diff_hunk}\n\n--- AST SUMMARY ---\n{ast_summary}\n\n--- BLAST RADIUS ---\n{blast_radius}\n\n--- REPO CONVENTIONS ---\n{repo_conventions}"
            elif agent == "architecture_agent":
                prompt_inst = ARCHITECTURE_PROMPT.split("--- DIFF HUNK ---")[0].strip()
                prompt_input = f"--- DIFF HUNK ---\n{diff_hunk}\n\n--- BLAST RADIUS ---\n{blast_radius}\n\n--- REPO CONVENTIONS ---\n{repo_conventions}"
            elif agent == "test_coverage_agent":
                prompt_inst = TEST_COVERAGE_PROMPT.split("--- DIFF HUNK (source) ---")[0].strip()
                prompt_input = f"--- DIFF HUNK (source) ---\n{diff_hunk}\n\n--- DIFF HUNK (tests changed in this PR, if any) ---\nNo test files changed in this PR."
            else:
                continue

            jsonl_records.append({
                "instruction": prompt_inst,
                "input": prompt_input,
                "output": output_json
            })
            
    except Exception as exc:
        print(f"[!] Database read failed or empty: {exc}. Proceeding with synthetic generation.")

    # 2. Add public dataset examples (online search / local fallbacks)
    print("[*] Blending with public code review dataset samples...")
    # Attempt to load from HuggingFace dataset if available and internet is active
    try:
        from datasets import load_dataset
        # Load a small code feedback instruction dataset for blending
        dataset = load_dataset("m-a-p/CodeFeedback-Filter-v1", split="train", streaming=True)
        count = 0
        for item in dataset:
            if count >= 30:  # Blend 30 high quality public instructions
                break
            query = item.get("query", "")
            answer = item.get("answer", "")
            if len(query) < 1000 and len(answer) < 1000:
                jsonl_records.append({
                    "instruction": "Explain the following code snippet and perform code review suggestions.",
                    "input": query,
                    "output": answer
                })
                count += 1
        print(f"[*] Blended {count} HuggingFace public samples.")
    except Exception as e:
        print(f"[*] HuggingFace datasets library offline or skipped ({e}). Using local high-quality public fallbacks.")
        # Load from local offline public samples
        for sample in OFFLINE_PUBLIC_SAMPLES:
            agent = sample["agent"]
            diff = sample["diff_hunk"]
            ast = sample.get("ast_summary", "")
            blast = ", ".join(sample.get("blast_radius", [])) or "None"
            convs = sample.get("repo_conventions", "")
            out_json = json.dumps({"findings": sample["findings"]})

            if agent == "security_agent":
                prompt_inst = SECURITY_PROMPT.split("--- DIFF HUNK ---")[0].strip()
                prompt_input = f"--- DIFF HUNK ---\n{diff}\n\n--- AST SUMMARY ---\n{ast}\n\n--- BLAST RADIUS ---\n{blast}\n\n--- REPO CONVENTIONS ---\n{convs}"
            elif agent == "architecture_agent":
                prompt_inst = ARCHITECTURE_PROMPT.split("--- DIFF HUNK ---")[0].strip()
                prompt_input = f"--- DIFF HUNK ---\n{diff}\n\n--- BLAST RADIUS ---\n{blast}\n\n--- REPO CONVENTIONS ---\n{convs}"
            elif agent == "test_coverage_agent":
                prompt_inst = TEST_COVERAGE_PROMPT.split("--- DIFF HUNK (source) ---")[0].strip()
                prompt_input = f"--- DIFF HUNK (source) ---\n{diff}\n\n--- DIFF HUNK (tests changed in this PR, if any) ---\n{sample.get('test_diff_hunks', 'No test files changed.')}"
            else:
                continue

            jsonl_records.append({
                "instruction": prompt_inst,
                "input": prompt_input,
                "output": out_json
            })
        print(f"[*] Blended {len(OFFLINE_PUBLIC_SAMPLES)} local offline public samples.")

    # 3. Save to training_data.jsonl
    output_file = backend_path / "scripts" / "training_data.jsonl"
    with open(output_file, "w", encoding="utf-8") as f:
        for record in jsonl_records:
            f.write(json.dumps(record) + "\n")

    print(f"[*] Success! Saved {len(jsonl_records)} training records to {output_file}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
training/evaluate.py — Go/no-go evaluation gate.

Compares the fine-tuned model (via vLLM) against the Groq baseline on the
held-out eval_set.jsonl that train_lora.py saved before training.

Metrics computed per model:
  - Precision: of findings raised, how many match a real labeled finding
  - Recall:    of real labeled findings, how many were caught
  - F1:        harmonic mean
  - FP rate:   on clean examples (0 real findings), rate of hallucinated findings

Go/no-go gate (exits with code 0 = PASS, 1 = FAIL):
  PASS if fine-tuned recall >= Groq recall
         AND fine-tuned FP rate <= Groq FP rate * 1.2 (at most 20% worse)
  FAIL otherwise.

Usage:
    python training/evaluate.py [--eval-set PATH] [--output-report PATH]

    # To run against live models you need:
    #   MODEL_BACKEND=vllm   and vLLM running at VLLM_API_URL
    #   GROQ_API_KEY         set in environment

    # Dry run (mocked responses) for CI:
    python training/evaluate.py --dry-run
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

# Tolerance: fine-tuned FP rate must be no more than 20% worse than Groq's
FP_RATE_SLACK = 1.20


# ── Metric helpers ────────────────────────────────────────────────────────────

def compute_metrics(
    predictions: list[dict],
    references: list[dict],
    is_clean_mask: list[bool],
) -> dict[str, float]:
    """
    Compute precision, recall, F1 and false-positive rate.

    Args:
        predictions:   list of {"findings": [...]} model outputs
        references:    list of {"findings": [...]} gold labels
        is_clean_mask: parallel bool list — True where the gold label has 0 findings
    """
    tp = fp = fn = 0
    fp_on_clean = 0
    clean_count = sum(is_clean_mask)

    for pred, ref, is_clean in zip(predictions, references, is_clean_mask):
        pred_findings = pred.get("findings", [])
        ref_findings = ref.get("findings", [])
        matched_refs: set[int] = set()

        for pf in pred_findings:
            p_line = pf.get("line")
            p_cat = (pf.get("category") or "").lower()
            matched = False
            for r_idx, rf in enumerate(ref_findings):
                r_line = rf.get("line")
                r_cat = (rf.get("category") or "").lower()
                line_ok = (
                    (p_line is not None and r_line is not None and abs(p_line - r_line) <= 5)
                    or (p_line is None and r_line is None)
                )
                if line_ok and (not p_cat or not r_cat or p_cat == r_cat):
                    matched = True
                    matched_refs.add(r_idx)
                    break
            tp += int(matched)
            fp += int(not matched)
            if is_clean and not matched:
                fp_on_clean += 1

        fn += len(ref_findings) - len(matched_refs)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = 2 * precision * recall / (precision + recall + 1e-9)
    fp_rate = fp_on_clean / clean_count if clean_count > 0 else 0.0

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "fp_rate_on_clean": fp_rate,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "clean_examples": clean_count,
        "fp_on_clean": fp_on_clean,
    }


# ── Model call helpers ────────────────────────────────────────────────────────

def call_vllm(prompt: str) -> dict:
    """Call the self-hosted vLLM endpoint."""
    import httpx
    from app.config import get_settings
    settings = get_settings()
    url = f"{settings.vllm_api_url}/chat/completions"
    payload = {
        "model": settings.vllm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def call_groq(prompt: str) -> dict:
    """Call the Groq baseline model."""
    from groq import Groq
    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        raise RuntimeError("GROQ_API_KEY not set. Required for baseline evaluation.")
    client = Groq(api_key=groq_api_key)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def call_dry_run(prompt: str, model_name: str, idx: int) -> dict:
    """
    Deterministic mock for CI / dry-run mode.
    Fine-tuned model: simulates ~90% recall, ~10% FP rate on clean.
    Groq baseline:    simulates ~85% recall, ~8% FP rate on clean.
    """
    seed = hash(prompt[:50]) % 100
    if model_name == "finetuned":
        # Slightly better recall, slightly more FP
        is_correct = (seed % 10) != 0
        is_fp = (seed % 11) == 0
    else:
        is_correct = (seed % 10) not in (0, 1)
        is_fp = (seed % 13) == 0
    return {"findings": [{"line": 5, "severity": "warning", "message": "mock", "confidence": 0.8}]} if (is_correct or is_fp) else {"findings": []}


# ── Evaluation runner ─────────────────────────────────────────────────────────

def run_evaluation(
    eval_data: list[dict],
    dry_run: bool,
) -> tuple[dict[str, float], dict[str, float]]:
    """
    Run both models over all eval examples.
    Returns (finetuned_metrics, groq_metrics).
    """
    finetuned_preds: list[dict] = []
    groq_preds: list[dict] = []
    references: list[dict] = []
    is_clean_mask: list[bool] = []

    total = len(eval_data)
    print(f"\n[*] Evaluating {total} examples against both models...")

    for idx, item in enumerate(eval_data):
        prompt = item["instruction"] + "\n" + item["input"]
        ref = json.loads(item["output"])
        references.append(ref)
        is_clean_mask.append(len(ref.get("findings", [])) == 0)

        # Fine-tuned (vLLM)
        try:
            ft_result = call_dry_run(prompt, "finetuned", idx) if dry_run else call_vllm(prompt)
        except Exception as exc:
            logger.warning("finetuned_model_call_failed", idx=idx, error=str(exc))
            ft_result = {"findings": []}
        finetuned_preds.append(ft_result)

        # Groq baseline
        try:
            gr_result = call_dry_run(prompt, "groq", idx) if dry_run else call_groq(prompt)
        except Exception as exc:
            logger.warning("groq_call_failed", idx=idx, error=str(exc))
            gr_result = {"findings": []}
        groq_preds.append(gr_result)

        if (idx + 1) % 5 == 0 or (idx + 1) == total:
            print(f"  Progress: {idx + 1}/{total}", end="\r", flush=True)
        if not dry_run:
            time.sleep(0.3)  # rate limit courtesy pause

    print()
    ft_metrics = compute_metrics(finetuned_preds, references, is_clean_mask)
    gr_metrics = compute_metrics(groq_preds, references, is_clean_mask)
    return ft_metrics, gr_metrics


# ── Gate & report ─────────────────────────────────────────────────────────────

def format_report(ft: dict, gr: dict, gate_passed: bool, dry_run: bool) -> str:
    mode_note = " *(dry-run / simulated values)*" if dry_run else ""
    gate_str = "✅ GATE PASSED" if gate_passed else "❌ GATE FAILED"

    return f"""# Phase 6 — Fine-Tuned Model Evaluation Report{mode_note}

## Summary

| Metric | Fine-Tuned (vLLM) | Groq Baseline | Delta |
| :--- | :---: | :---: | :---: |
| **Precision** | {ft['precision']:.3f} | {gr['precision']:.3f} | {ft['precision'] - gr['precision']:+.3f} |
| **Recall** | {ft['recall']:.3f} | {gr['recall']:.3f} | {ft['recall'] - gr['recall']:+.3f} |
| **F1** | {ft['f1']:.3f} | {gr['f1']:.3f} | {ft['f1'] - gr['f1']:+.3f} |
| **FP Rate (clean examples)** | {ft['fp_rate_on_clean']:.3f} | {gr['fp_rate_on_clean']:.3f} | {ft['fp_rate_on_clean'] - gr['fp_rate_on_clean']:+.3f} |
| True Positives | {ft['tp']} | {gr['tp']} | — |
| False Positives | {ft['fp']} | {gr['fp']} | — |
| False Negatives | {ft['fn']} | {gr['fn']} | — |
| FP on clean examples | {ft['fp_on_clean']}/{ft['clean_examples']} | {gr['fp_on_clean']}/{gr['clean_examples']} | — |

## Go/No-Go Gate

**Gate conditions:**
1. Fine-tuned Recall ≥ Groq Recall
2. Fine-tuned FP Rate ≤ Groq FP Rate × {FP_RATE_SLACK:.0%} (at most {(FP_RATE_SLACK-1)*100:.0f}% worse)

**Result: {gate_str}**

{"✅ Condition 1 (Recall): PASS — " if ft['recall'] >= gr['recall'] else "❌ Condition 1 (Recall): FAIL — "}{ft['recall']:.3f} vs {gr['recall']:.3f}
{"✅ Condition 2 (FP Rate): PASS — " if ft['fp_rate_on_clean'] <= gr['fp_rate_on_clean'] * FP_RATE_SLACK else "❌ Condition 2 (FP Rate): FAIL — "}{ft['fp_rate_on_clean']:.3f} vs {gr['fp_rate_on_clean']:.3f} (threshold: {gr['fp_rate_on_clean'] * FP_RATE_SLACK:.3f})

{"**Next step:** Update MODEL_BACKEND=vllm in .env to route production traffic to the fine-tuned model." if gate_passed else "**Next step:** Do NOT update MODEL_BACKEND to vllm. Analyze the FP/FN breakdown above to identify whether more training data, longer training, or a different architecture is needed. Document findings in the README."}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate fine-tuned vs. Groq baseline with go/no-go gate.")
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=root_path / "training" / "eval_set.jsonl",
        help="Path to the held-out eval set from train_lora.py.",
    )
    parser.add_argument(
        "--output-report",
        type=Path,
        default=root_path / "docs" / "evaluation_report.md",
        help="Path to write the markdown evaluation report.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use mocked model responses instead of live API calls (for CI / local testing).",
    )
    args = parser.parse_args()

    print("=" * 80)
    print(" PHASE 6 — FINE-TUNED VS. GROQ BASELINE EVALUATION")
    print("=" * 80)
    if args.dry_run:
        print("[*] DRY RUN MODE — using simulated model responses.")

    if not args.eval_set.exists():
        print(f"[!] Eval set not found at {args.eval_set}.")
        print("    Run training/train_lora.py first — it saves the held-out set.")
        sys.exit(1)

    with open(args.eval_set, "r", encoding="utf-8") as f:
        eval_data = [json.loads(line) for line in f]

    print(f"[*] Loaded {len(eval_data)} eval examples.")

    ft_metrics, gr_metrics = run_evaluation(eval_data, dry_run=args.dry_run)

    # Gate logic
    recall_passes = ft_metrics["recall"] >= gr_metrics["recall"]
    fp_passes = ft_metrics["fp_rate_on_clean"] <= gr_metrics["fp_rate_on_clean"] * FP_RATE_SLACK
    gate_passed = recall_passes and fp_passes

    # Print table
    print("\n" + "=" * 60)
    print(f"  Metric          | Fine-Tuned | Groq Baseline")
    print(f"  {'Precision':15} | {ft_metrics['precision']:.3f}      | {gr_metrics['precision']:.3f}")
    print(f"  {'Recall':15} | {ft_metrics['recall']:.3f}      | {gr_metrics['recall']:.3f}")
    print(f"  {'F1':15} | {ft_metrics['f1']:.3f}      | {gr_metrics['f1']:.3f}")
    print(f"  {'FP Rate (clean)':15} | {ft_metrics['fp_rate_on_clean']:.3f}      | {gr_metrics['fp_rate_on_clean']:.3f}")
    print("=" * 60)

    if gate_passed:
        print("\n[GATE PASSED] ✅ Fine-tuned model meets or exceeds Groq baseline.")
        print("  → You may now set MODEL_BACKEND=vllm in .env to route production traffic.")
    else:
        print("\n[GATE FAILED] ❌ Fine-tuned model does not meet the production bar.")
        if not recall_passes:
            print(f"  → Recall: {ft_metrics['recall']:.3f} < {gr_metrics['recall']:.3f} (Groq baseline)")
        if not fp_passes:
            print(
                f"  → FP Rate: {ft_metrics['fp_rate_on_clean']:.3f} > "
                f"{gr_metrics['fp_rate_on_clean'] * FP_RATE_SLACK:.3f} (threshold)"
            )
        print("  → Do NOT change MODEL_BACKEND to vllm. Document this honestly in the README.")

    # Save report
    report = format_report(ft_metrics, gr_metrics, gate_passed, dry_run=args.dry_run)
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    args.output_report.write_text(report, encoding="utf-8")
    print(f"\n[*] Report written to {args.output_report}")

    sys.exit(0 if gate_passed else 1)


if __name__ == "__main__":
    main()

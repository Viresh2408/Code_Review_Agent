"""
backend/tests/test_evaluate_metrics.py

Tests for the metric computation in training/evaluate.py.
Uses purely synthetic data — no API calls, no vLLM, no Groq.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

root_path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(root_path / "backend"))

from training.evaluate import compute_metrics, FP_RATE_SLACK


# ── compute_metrics tests ─────────────────────────────────────────────────────

def test_perfect_predictions():
    """All predictions exactly match references → precision=1.0, recall=1.0, F1=1.0."""
    predictions = [{"findings": [{"line": 5, "severity": "warning", "message": "x", "category": "security", "confidence": 0.9}]}]
    references =  [{"findings": [{"line": 5, "severity": "warning", "message": "x", "category": "security", "confidence": 0.9}]}]
    is_clean = [False]

    m = compute_metrics(predictions, references, is_clean)

    assert m["precision"] == pytest.approx(1.0)
    assert m["recall"] == pytest.approx(1.0)
    assert m["f1"] == pytest.approx(1.0)
    assert m["fp_rate_on_clean"] == pytest.approx(0.0)


def test_all_false_positives_no_real_findings():
    """
    Model raises findings but none match references.
    → precision=0, recall=0 (no real findings to catch either).
    """
    predictions = [{"findings": [{"line": 3, "severity": "nit", "message": "y", "category": "style", "confidence": 0.5}]}]
    references =  [{"findings": [{"line": 99, "severity": "blocker", "message": "z", "category": "security", "confidence": 0.9}]}]
    is_clean = [False]

    m = compute_metrics(predictions, references, is_clean)

    assert m["precision"] == pytest.approx(0.0)
    assert m["tp"] == 0
    assert m["fp"] == 1
    assert m["fn"] == 1


def test_false_positive_rate_on_clean_examples():
    """
    For examples with zero real findings, any prediction is a false positive.
    FP rate = fp_on_clean / clean_example_count.
    """
    # 2 clean examples, model hallucinates on 1 of them
    predictions = [
        {"findings": [{"line": 5, "severity": "nit", "message": "spurious", "category": "style", "confidence": 0.6}]},
        {"findings": []},
    ]
    references = [
        {"findings": []},
        {"findings": []},
    ]
    is_clean = [True, True]

    m = compute_metrics(predictions, references, is_clean)

    assert m["clean_examples"] == 2
    assert m["fp_on_clean"] == 1
    assert m["fp_rate_on_clean"] == pytest.approx(0.5)


def test_recall_partial():
    """Model misses one of two real findings → recall = 0.5."""
    predictions = [{"findings": [{"line": 5, "severity": "warning", "message": "a", "category": "security", "confidence": 0.8}]}]
    references =  [{"findings": [
        {"line": 5,  "severity": "warning", "message": "a", "category": "security", "confidence": 0.8},
        {"line": 20, "severity": "blocker", "message": "b", "category": "security", "confidence": 0.95},
    ]}]
    is_clean = [False]

    m = compute_metrics(predictions, references, is_clean)

    assert m["tp"] == 1
    assert m["fn"] == 1
    assert m["recall"] == pytest.approx(0.5)


def test_line_tolerance_within_5():
    """A prediction within ±5 lines of a reference is considered a match."""
    predictions = [{"findings": [{"line": 8, "severity": "warning", "message": "x", "category": "security", "confidence": 0.9}]}]
    references =  [{"findings": [{"line": 5, "severity": "warning", "message": "x", "category": "security", "confidence": 0.9}]}]
    is_clean = [False]

    m = compute_metrics(predictions, references, is_clean)

    # abs(8 - 5) = 3 ≤ 5 → should match
    assert m["tp"] == 1
    assert m["fp"] == 0


def test_line_tolerance_outside_5():
    """A prediction >5 lines away is NOT a match."""
    predictions = [{"findings": [{"line": 15, "severity": "warning", "message": "x", "category": "security", "confidence": 0.9}]}]
    references =  [{"findings": [{"line": 5, "severity": "warning", "message": "x", "category": "security", "confidence": 0.9}]}]
    is_clean = [False]

    m = compute_metrics(predictions, references, is_clean)

    # abs(15 - 5) = 10 > 5 → no match
    assert m["tp"] == 0
    assert m["fp"] == 1


# ── Gate logic tests ──────────────────────────────────────────────────────────

def test_gate_passes_when_finetuned_exceeds_groq():
    """Gate should pass when fine-tuned recall ≥ Groq recall and FP rate within slack."""
    ft = {"recall": 0.90, "fp_rate_on_clean": 0.05, "precision": 0.88, "f1": 0.89, "tp": 9, "fp": 1, "fn": 1, "clean_examples": 20, "fp_on_clean": 1}
    gr = {"recall": 0.85, "fp_rate_on_clean": 0.06, "precision": 0.84, "f1": 0.845, "tp": 9, "fp": 1, "fn": 1, "clean_examples": 20, "fp_on_clean": 1}

    recall_passes = ft["recall"] >= gr["recall"]
    fp_passes = ft["fp_rate_on_clean"] <= gr["fp_rate_on_clean"] * FP_RATE_SLACK
    gate_passed = recall_passes and fp_passes

    assert gate_passed is True


def test_gate_fails_when_recall_below_groq():
    """Gate should fail when fine-tuned recall < Groq recall."""
    ft = {"recall": 0.70, "fp_rate_on_clean": 0.03}
    gr = {"recall": 0.85, "fp_rate_on_clean": 0.05}

    recall_passes = ft["recall"] >= gr["recall"]
    assert recall_passes is False


def test_gate_fails_when_fp_rate_too_high():
    """Gate should fail when fine-tuned FP rate > Groq FP rate * FP_RATE_SLACK."""
    ft = {"recall": 0.90, "fp_rate_on_clean": 0.15}
    gr = {"recall": 0.85, "fp_rate_on_clean": 0.06}

    fp_passes = ft["fp_rate_on_clean"] <= gr["fp_rate_on_clean"] * FP_RATE_SLACK
    # 0.15 > 0.06 * 1.20 = 0.072 → should fail
    assert fp_passes is False


def test_gate_tolerates_fp_within_slack():
    """Gate should pass when fine-tuned FP rate is within the 20% slack of Groq."""
    ft = {"recall": 0.90, "fp_rate_on_clean": 0.065}
    gr = {"recall": 0.85, "fp_rate_on_clean": 0.060}

    # 0.065 ≤ 0.060 * 1.20 = 0.072 → should pass
    fp_passes = ft["fp_rate_on_clean"] <= gr["fp_rate_on_clean"] * FP_RATE_SLACK
    assert fp_passes is True

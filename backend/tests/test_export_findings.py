"""
backend/tests/test_export_findings.py

Tests for training/export_findings.py:
  - Idempotency: same DB state → identical JSONL output
  - Volume gate: < 200 own examples → exits with code 1 without --force
  - source field present in every output line
  - Rejected escalations produce negative examples (empty findings)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

root_path = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(root_path / "backend"))

from training.export_findings import (
    build_record,
    export_public_fallback,
    MINIMUM_OWN_DATA_EXAMPLES,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_finding(
    id_: int,
    agent: str = "security_agent",
    line: int = 10,
    severity: str = "warning",
    message: str = "Test finding",
    confidence: float = 0.85,
    escalated: bool = False,
    outcome: str = "n/a",
) -> MagicMock:
    """Create a mock Finding ORM object."""
    f = MagicMock()
    f.id = id_
    f.agent = agent
    f.line_number = line
    f.severity = severity
    f.message = message
    f.confidence = confidence
    f.escalated_to_claude = escalated
    f.escalation_outcome = outcome
    f.suggested_fix = None
    f.file_path = f"src/module_{id_}.py"
    f.review_id = 1
    return f


# ── Test: idempotency ─────────────────────────────────────────────────────────

def test_idempotency(tmp_path):
    """
    Running export_own_findings against the same ordered list of findings
    produces byte-for-byte identical output on two separate calls.
    """
    from training.export_findings import export_own_findings, generate_synthetic_context

    findings = [_make_finding(i) for i in range(1, 6)]

    def make_session():
        session = MagicMock()
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = findings
        session.execute.return_value = exec_result
        return session

    records_run1 = export_own_findings(make_session())
    records_run2 = export_own_findings(make_session())

    assert records_run1 == records_run2, (
        "export_own_findings is not deterministic — same input produced different output"
    )

    # Serializing to JSONL and hashing must also be identical
    def to_bytes(records):
        return b"\n".join(json.dumps(r).encode() for r in records) + b"\n"

    assert hashlib.sha256(to_bytes(records_run1)).hexdigest() == hashlib.sha256(to_bytes(records_run2)).hexdigest()


# ── Test: volume gate ─────────────────────────────────────────────────────────

def test_volume_gate_exits_without_force(tmp_path, capsys):
    """
    When own_count < MINIMUM_OWN_DATA_EXAMPLES and --force is not passed,
    main() must exit with code 1.
    """
    from training.export_findings import main as export_main

    few_records = [_make_finding(i) for i in range(5)]

    with (
        patch("training.export_findings.create_engine"),
        patch("training.export_findings.sessionmaker") as mock_session_factory,
        patch("training.export_findings.export_own_findings", return_value=[
            {"instruction": "i", "input": "x", "output": '{"findings":[]}', "source": "own_findings"}
            for _ in range(10)  # 10 own examples < 200
        ]),
        patch("training.export_findings.export_public_fallback", return_value=[]),
        patch("builtins.open", MagicMock()),
    ):
        with pytest.raises(SystemExit) as exc_info:
            export_main(output_path=tmp_path / "out.jsonl", force=False)
        assert exc_info.value.code == 1


def test_volume_gate_bypassed_with_force(tmp_path):
    """With --force, the volume gate is bypassed and the script continues."""
    from training.export_findings import main as export_main

    few_own_records = [
        {"instruction": "i", "input": "x", "output": '{"findings":[]}', "source": "own_findings"}
        for _ in range(10)
    ]

    with (
        patch("training.export_findings.create_engine"),
        patch("training.export_findings.sessionmaker"),
        patch("training.export_findings.export_own_findings", return_value=few_own_records),
        patch("training.export_findings.export_public_fallback", return_value=[]),
    ):
        # Should NOT raise SystemExit
        out_file = tmp_path / "out.jsonl"
        export_main(output_path=out_file, force=True)
        assert out_file.exists()


# ── Test: source field ────────────────────────────────────────────────────────

def test_source_field_present_own_findings():
    """Every own-findings record has source='own_findings'."""
    from training.export_findings import export_own_findings, generate_synthetic_context

    findings = [_make_finding(i) for i in range(1, 4)]
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = findings
    session.execute.return_value = exec_result

    records = export_own_findings(session)
    for record in records:
        assert "source" in record, f"Missing 'source' field in record: {record}"
        assert record["source"] == "own_findings", f"Expected 'own_findings', got: {record['source']}"


def test_source_field_present_public_dataset():
    """Every public-dataset record has source='public_dataset'."""
    records = export_public_fallback()
    assert len(records) > 0, "No public fallback records returned"
    for record in records:
        assert "source" in record
        assert record["source"] == "public_dataset"


# ── Test: negative examples ───────────────────────────────────────────────────

def test_rejected_escalation_produces_empty_findings():
    """
    A finding with escalated_to_claude=True and escalation_outcome='rejected'
    must produce {"findings": []} as the training output (negative example).
    """
    from training.export_findings import export_own_findings

    rejected_finding = _make_finding(
        id_=999,
        agent="security_agent",
        escalated=True,
        outcome="rejected",
    )

    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = [rejected_finding]
    session.execute.return_value = exec_result

    records = export_own_findings(session)
    assert len(records) == 1, f"Expected 1 record, got {len(records)}"
    output = json.loads(records[0]["output"])
    assert output == {"findings": []}, (
        f"Rejected escalation should produce empty findings, got: {output}"
    )


def test_confirmed_escalation_produces_positive_example():
    """
    A finding with escalated_to_claude=True and escalation_outcome='confirmed'
    must produce a positive training example (findings not empty).
    """
    from training.export_findings import export_own_findings

    confirmed_finding = _make_finding(
        id_=998,
        agent="security_agent",
        message="SQL injection vulnerability",
        escalated=True,
        outcome="confirmed",
    )

    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = [confirmed_finding]
    session.execute.return_value = exec_result

    records = export_own_findings(session)
    assert len(records) == 1
    output = json.loads(records[0]["output"])
    assert len(output.get("findings", [])) > 0, (
        "Confirmed escalation should produce a positive example (non-empty findings)"
    )


def test_unknown_escalation_outcome_skipped():
    """
    A finding with escalated_to_claude=True and escalation_outcome='n/a'
    should be skipped entirely (unknown outcome = noisy data).
    """
    from training.export_findings import export_own_findings

    unknown_finding = _make_finding(
        id_=997,
        agent="security_agent",
        escalated=True,
        outcome="n/a",
    )

    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = [unknown_finding]
    session.execute.return_value = exec_result

    records = export_own_findings(session)
    assert len(records) == 0, (
        "Findings with escalation_outcome='n/a' should be skipped to avoid noisy training data"
    )

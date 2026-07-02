"""
Unit tests for the LangGraph orchestrator nodes (Security Agent and Aggregator).
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from agents.orchestrator import aggregator_node, security_agent_node
from agents.schemas import ChangedFile, Finding, PRContext

# ── Aggregator Node Tests ──────────────────────────────────────────────────────

def test_aggregator_deduplication():
    """Verify that deduplication keeps the finding with the highest confidence."""
    findings = [
        Finding(
            agent="security_agent",
            file_path="src/auth.py",
            line=10,
            severity="blocker",
            category="security",
            message="SQL Injection suspected (low confidence)",
            confidence=0.45,
            suggested_fix="Use params",
            escalated_to_claude=False
        ),
        Finding(
            agent="security_agent",
            file_path="src/auth.py",
            line=10,
            severity="blocker",
            category="security",
            message="SQL Injection confirmed (high confidence)",
            confidence=0.92,
            suggested_fix="Use params",
            escalated_to_claude=True
        ),
        Finding(
            agent="security_agent",
            file_path="src/auth.py",
            line=10,
            severity="nit",
            category="style",  # different category -> should keep both
            message="Whitespace styling",
            confidence=0.80,
            suggested_fix=None,
            escalated_to_claude=False
        )
    ]
    state = PRContext(
        repo="owner/repo",
        pr_number=1,
        commit_sha="sha",
        changed_files=[],
        repo_conventions="",
        findings=findings
    )

    result = aggregator_node(state)
    result_findings = result["findings"]

    # There should be exactly 2 findings after deduplication
    assert len(result_findings) == 2

    # Find the security category one and assert it is the high confidence one (0.92)
    sec_finding = next(f for f in result_findings if f.category == "security")
    assert sec_finding.confidence == 0.92
    assert sec_finding.message == "SQL Injection confirmed (high confidence)"

    # Style category finding should be preserved
    style_finding = next(f for f in result_findings if f.category == "style")
    assert style_finding.confidence == 0.80


def test_aggregator_sorting():
    """Verify that sorting order is correct (blocker > warning > nit)."""
    findings = [
        Finding(
            agent="security_agent",
            file_path="src/1.py",
            line=1,
            severity="nit",
            category="sec",
            message="Nit finding",
            confidence=0.9,
            escalated_to_claude=False
        ),
        Finding(
            agent="security_agent",
            file_path="src/2.py",
            line=2,
            severity="blocker",
            category="sec",
            message="Blocker finding",
            confidence=0.9,
            escalated_to_claude=False
        ),
        Finding(
            agent="security_agent",
            file_path="src/3.py",
            line=3,
            severity="warning",
            category="sec",
            message="Warning finding",
            confidence=0.9,
            escalated_to_claude=False
        )
    ]
    state = PRContext(
        repo="owner/repo",
        pr_number=1,
        commit_sha="sha",
        changed_files=[],
        repo_conventions="",
        findings=findings
    )

    result = aggregator_node(state)
    result_findings = result["findings"]

    # Should be sorted: blocker (src/2.py) -> warning (src/3.py) -> nit (src/1.py)
    assert len(result_findings) == 3
    assert result_findings[0].file_path == "src/2.py"
    assert result_findings[0].severity == "blocker"
    assert result_findings[1].file_path == "src/3.py"
    assert result_findings[1].severity == "warning"
    assert result_findings[2].file_path == "src/1.py"
    assert result_findings[2].severity == "nit"


def test_aggregator_no_capping_in_node():
    """Verify that it does NOT truncate findings in the aggregator node itself."""
    findings = [
        Finding(
            agent="security_agent",
            file_path=f"src/{i}.py",
            line=i,
            severity="warning",
            category="sec",
            message=f"Warning finding {i}",
            confidence=0.9,
            escalated_to_claude=False
        )
        for i in range(25)
    ]
    state = PRContext(
        repo="owner/repo",
        pr_number=1,
        commit_sha="sha",
        changed_files=[],
        repo_conventions="",
        findings=findings
    )

    result = aggregator_node(state)
    result_findings = result["findings"]

    assert len(result_findings) == 25


def test_aggregator_empty_findings():
    """Verify that an empty findings list doesn't crash the aggregator."""
    state = PRContext(
        repo="owner/repo",
        pr_number=1,
        commit_sha="sha",
        changed_files=[],
        repo_conventions="",
        findings=[]
    )

    result = aggregator_node(state)
    result_findings = result["findings"]
    assert result_findings == []


# ── Security Agent Escalation Tests ───────────────────────────────────────────

@patch("agents.orchestrator.Groq")
@patch("agents.orchestrator.Anthropic")
@patch.dict(
    os.environ,
    {"GROQ_API_KEY": "test-groq-key", "ANTHROPIC_API_KEY": "test-anthropic-key"},
)
def test_security_agent_node_escalation(mock_anthropic_class, mock_groq_class):
    """Verify that low confidence triggers Claude escalation and confirmed finding is kept."""
    mock_groq_client = MagicMock()
    mock_groq_class.return_value = mock_groq_client

    mock_anthropic_client = MagicMock()
    mock_anthropic_class.return_value = mock_anthropic_client

    # Groq returns:
    # 1. High confidence finding (0.95) -> should bypass escalation
    # 2. Low confidence finding (0.50) -> should trigger escalation
    mock_groq_response = MagicMock()
    mock_groq_response.usage.prompt_tokens = 100
    mock_groq_response.usage.completion_tokens = 50
    mock_groq_response.choices = [
        MagicMock(message=MagicMock(content=json.dumps({
            "findings": [
                {
                    "line": 10,
                    "severity": "blocker",
                    "message": "High confidence security issue",
                    "confidence": 0.95,
                    "suggested_fix": "Fix SQL injection"
                },
                {
                    "line": 20,
                    "severity": "warning",
                    "message": "Low confidence security issue",
                    "confidence": 0.50,
                    "suggested_fix": "Fix hardcoded secret"
                }
            ]
        })))
    ]
    mock_groq_client.chat.completions.create.return_value = mock_groq_response

    # Anthropic returns:
    # Confirmed/Refined finding
    mock_anthropic_response = MagicMock()
    mock_anthropic_response.usage.input_tokens = 150
    mock_anthropic_response.usage.output_tokens = 75
    mock_anthropic_response.content = [
        MagicMock(text=json.dumps({
            "findings": [
                {
                    "line": 20,
                    "severity": "blocker",  # Claude escalates severity
                    "message": "Confirmed low confidence issue",
                    "confidence": 0.85,
                    "suggested_fix": "Fix hardcoded secret"
                }
            ]
        }))
    ]
    mock_anthropic_client.messages.create.return_value = mock_anthropic_response

    # Construct input context
    state = PRContext(
        repo="owner/repo",
        pr_number=42,
        commit_sha="abcdef123456",
        changed_files=[
            ChangedFile(
                path="src/auth.py",
                language="python",
                diff_hunks=["@@ -5,20 +5,25 @@\n..."],
                ast_summary="def test(): pass",
                blast_radius=[]
            )
        ],
        repo_conventions="Use parameterized queries."
    )

    # Act
    result = security_agent_node(state)

    # Assert
    findings = result["findings"]
    assert len(findings) == 2

    # Check high confidence finding (not escalated)
    f1 = next(f for f in findings if f.line == 10)
    assert f1.severity == "blocker"
    assert f1.message == "High confidence security issue"
    assert f1.confidence == 0.95
    assert f1.escalated_to_claude is False

    # Check escalated and confirmed finding
    f2 = next(f for f in findings if f.line == 20)
    assert f2.severity == "blocker"  # updated by Claude
    assert f2.message == "Confirmed low confidence issue"
    assert f2.confidence == 0.85
    assert f2.escalated_to_claude is True

    # Verify calls
    assert mock_groq_client.chat.completions.create.call_count == 1
    assert mock_anthropic_client.messages.create.call_count == 1


@patch("agents.orchestrator.Groq")
@patch("agents.orchestrator.Anthropic")
@patch.dict(
    os.environ,
    {"GROQ_API_KEY": "test-groq-key", "ANTHROPIC_API_KEY": "test-anthropic-key"},
)
def test_security_agent_node_escalation_rejection(mock_anthropic_class, mock_groq_class):
    """Verify that if Claude rejects a finding (returns empty findings), it is skipped."""
    mock_groq_client = MagicMock()
    mock_groq_class.return_value = mock_groq_client

    mock_anthropic_client = MagicMock()
    mock_anthropic_class.return_value = mock_anthropic_client

    mock_groq_response = MagicMock()
    mock_groq_response.usage.prompt_tokens = 100
    mock_groq_response.usage.completion_tokens = 50
    mock_groq_response.choices = [
        MagicMock(message=MagicMock(content=json.dumps({
            "findings": [
                {
                    "line": 30,
                    "severity": "warning",
                    "message": "False positive candidate",
                    "confidence": 0.40,
                    "suggested_fix": None
                }
            ]
        })))
    ]
    mock_groq_client.chat.completions.create.return_value = mock_groq_response

    # Anthropic returns empty list (rejection)
    mock_anthropic_response = MagicMock()
    mock_anthropic_response.usage.input_tokens = 150
    mock_anthropic_response.usage.output_tokens = 75
    mock_anthropic_response.content = [
        MagicMock(text=json.dumps({
            "findings": []
        }))
    ]
    mock_anthropic_client.messages.create.return_value = mock_anthropic_response

    state = PRContext(
        repo="owner/repo",
        pr_number=42,
        commit_sha="abcdef123456",
        changed_files=[
            ChangedFile(
                path="src/auth.py",
                language="python",
                diff_hunks=["@@ -5,20 +5,25 @@\n..."],
                ast_summary="def test(): pass",
                blast_radius=[]
            )
        ],
        repo_conventions="Use parameterized queries."
    )

    result = security_agent_node(state)
    findings = result["findings"]

    # Rejection should result in the finding being skipped (not appended)
    assert len(findings) == 0
    assert mock_groq_client.chat.completions.create.call_count == 1
    assert mock_anthropic_client.messages.create.call_count == 1

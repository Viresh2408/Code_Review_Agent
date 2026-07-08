"""
Unit tests for the LangGraph orchestrator nodes (Security Agent and Aggregator).
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from agents.orchestrator import aggregator_node, security_agent_node
from agents.schemas import ChangedFile, Finding, PRContext
import pytest

@pytest.fixture(autouse=True)
def mock_settings_groq_backend():
    """Force model_backend = 'groq' for all orchestrator tests to prevent live vLLM calls."""
    mock_settings = MagicMock()
    mock_settings.model_backend = "groq"
    mock_settings.anthropic_api_key = "test-anthropic-key"
    mock_settings.vllm_model = "qwen-test"
    mock_settings.vllm_gpu_cost_per_token = 0.000001
    with patch("agents.orchestrator.get_settings", return_value=mock_settings):
        yield

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


# ── LangGraph Parallel execution & Aggregator execution tests ─────────────────

import time
from langgraph.graph import StateGraph, END
from agents.schemas import ReplaceFindings, merge_findings
from agents.orchestrator import (
    architecture_agent_node,
    test_coverage_agent_node as coverage_agent_node_prod,
    debt_scoring_agent_node,
)

def test_aggregator_single_execution_staggered():
    """
    Verify that in a parallel fan-out/fan-in graph identical to our pipeline:
    - The aggregator node is invoked exactly once.
    - Aggregator receives findings from all 4 branches.
    - Custom merge_findings reducer and ReplaceFindings work correctly.
    """
    # 1. Setup a StateGraph with the same wiring as orchestrator
    test_workflow = StateGraph(PRContext)
    
    # We will record invocations
    call_log = []
    
    def slow_sec_node(state: PRContext):
        time.sleep(0.15)  # Simulate slow agent
        call_log.append("security")
        return {"findings": [Finding(
            agent="security_agent", file_path="a.py", severity="blocker",
            category="security", message="SQLi", confidence=1.0
        )]}
        
    def fast_arch_node(state: PRContext):
        call_log.append("architecture")
        return {"findings": [Finding(
            agent="architecture_agent", file_path="b.py", severity="warning",
            category="architecture", message="Circular Import", confidence=0.9
        )]}
        
    def slow_cov_node(state: PRContext):
        time.sleep(0.10)
        call_log.append("test_coverage")
        return {"findings": [Finding(
            agent="test_coverage_agent", file_path="c.py", severity="nit",
            category="test-coverage", message="No tests", confidence=0.8
        )]}
        
    def fast_debt_node(state: PRContext):
        call_log.append("debt_scoring")
        return {"debt_score_delta": 4.5}
        
    def mock_agg_node(state: PRContext):
        call_log.append("aggregator")
        # Assert that all agents have already finished before aggregator runs
        assert len(call_log) == 5  # 4 agents + aggregator itself
        assert call_log[-1] == "aggregator"
        
        # Deduplicate and overwrite using ReplaceFindings
        return {"findings": ReplaceFindings(state.findings)}

    def mock_ingestion(state: PRContext) -> dict:
        return {}

    # Wire up the test workflow identically
    test_workflow.add_node("ingestion", mock_ingestion)
    test_workflow.add_node("security", slow_sec_node)
    test_workflow.add_node("architecture", fast_arch_node)
    test_workflow.add_node("test_coverage", slow_cov_node)
    test_workflow.add_node("debt_scoring", fast_debt_node)
    test_workflow.add_node("aggregator", mock_agg_node)
    
    test_workflow.set_entry_point("ingestion")
    test_workflow.add_edge("ingestion", "security")
    test_workflow.add_edge("ingestion", "architecture")
    test_workflow.add_edge("ingestion", "test_coverage")
    test_workflow.add_edge("ingestion", "debt_scoring")
    
    test_workflow.add_edge("security", "aggregator")
    test_workflow.add_edge("architecture", "aggregator")
    test_workflow.add_edge("test_coverage", "aggregator")
    test_workflow.add_edge("debt_scoring", "aggregator")
    
    test_workflow.add_edge("aggregator", END)
    
    test_graph = test_workflow.compile()
    
    # 2. Run graph
    initial_state = PRContext(
        repo="owner/repo", pr_number=1, commit_sha="sha",
        changed_files=[], repo_conventions="", findings=[]
    )
    
    final_state = test_graph.invoke(initial_state)
    
    # Verify that the aggregator ran exactly once at the end
    assert call_log.count("aggregator") == 1
    assert call_log[-1] == "aggregator"
    
    # Verify findings from all nodes are present in the final state
    findings = final_state.findings if hasattr(final_state, "findings") else final_state.get("findings", [])
    assert len(findings) == 3
    agents = {f.agent for f in findings}
    assert agents == {"security_agent", "architecture_agent", "test_coverage_agent"}
    
    # Verify debt_score_delta is correct
    debt = final_state.debt_score_delta if hasattr(final_state, "debt_score_delta") else final_state.get("debt_score_delta", 0)
    assert debt == 4.5


# ── Architecture Agent Node Tests ─────────────────────────────────────────────

@patch("agents.orchestrator.Groq")
@patch("agents.orchestrator.Anthropic")
@patch.dict(
    os.environ,
    {"GROQ_API_KEY": "test-groq-key", "ANTHROPIC_API_KEY": "test-anthropic-key"},
)
def test_architecture_agent_node(mock_anthropic_class, mock_groq_class):
    """Verify that architecture agent node works and correctly triggers logs."""
    mock_groq_client = MagicMock()
    mock_groq_class.return_value = mock_groq_client
    
    # Mock Groq returns high confidence architectural finding
    mock_groq_response = MagicMock()
    mock_groq_response.usage.prompt_tokens = 80
    mock_groq_response.usage.completion_tokens = 40
    mock_groq_response.choices = [
        MagicMock(message=MagicMock(content=json.dumps({
            "findings": [
                {
                    "line": 5,
                    "severity": "warning",
                    "message": "Violation of architectural pattern",
                    "confidence": 0.9,
                    "suggested_fix": "Use helper instead of direct import"
                }
            ]
        })))
    ]
    mock_groq_client.chat.completions.create.return_value = mock_groq_response

    state = PRContext(
        repo="owner/repo",
        pr_number=1,
        commit_sha="sha",
        changed_files=[
            ChangedFile(
                path="src/app.py",
                language="python",
                diff_hunks=["@@ -1,10 +1,10 @@\n..."],
                ast_summary="",
                blast_radius=["func_name (caller.py)"]
            )
        ]
    )

    result = architecture_agent_node(state)
    findings = result["findings"]

    assert len(findings) == 1
    assert findings[0].agent == "architecture_agent"
    assert findings[0].line == 5
    assert findings[0].severity == "warning"
    assert findings[0].confidence == 0.9


# ── Test-Coverage Agent Node Tests ─────────────────────────────────────────────

@patch("agents.orchestrator.Groq")
@patch("agents.orchestrator.Anthropic")
@patch.dict(
    os.environ,
    {"GROQ_API_KEY": "test-groq-key", "ANTHROPIC_API_KEY": "test-anthropic-key"},
)
def test_test_coverage_agent_node(mock_anthropic_class, mock_groq_class):
    """Verify that test-coverage agent node works and correctly triggers logs."""
    mock_groq_client = MagicMock()
    mock_groq_class.return_value = mock_groq_client
    
    # Mock Groq returns missing test coverage finding
    mock_groq_response = MagicMock()
    mock_groq_response.usage.prompt_tokens = 90
    mock_groq_response.usage.completion_tokens = 30
    mock_groq_response.choices = [
        MagicMock(message=MagicMock(content=json.dumps({
            "findings": [
                {
                    "line": 15,
                    "severity": "warning",
                    "message": "Enclosing method lacks test coverage",
                    "confidence": 0.85,
                    "suggested_fix": None
                }
            ]
        })))
    ]
    mock_groq_client.chat.completions.create.return_value = mock_groq_response

    state = PRContext(
        repo="owner/repo",
        pr_number=1,
        commit_sha="sha",
        changed_files=[
            ChangedFile(
                path="src/logic.py",
                language="python",
                diff_hunks=["@@ -10,10 +10,10 @@\n..."],
                ast_summary="",
                blast_radius=[]
            ),
            ChangedFile(
                path="tests/test_logic.py",
                language="python",
                diff_hunks=["@@ -1,5 +1,5 @@\n..."],
                ast_summary="",
                blast_radius=[]
            )
        ]
    )

    result = coverage_agent_node_prod(state)
    findings = result["findings"]

    assert len(findings) == 1
    assert findings[0].agent == "test_coverage_agent"
    assert findings[0].line == 15
    assert findings[0].severity == "warning"
    assert findings[0].confidence == 0.85


# ── Debt-Scoring Agent Node Tests ─────────────────────────────────────────────

@patch("agents.orchestrator.call_primary_model")
@patch("agents.orchestrator.Anthropic")
@patch.dict(
    os.environ,
    {"ANTHROPIC_API_KEY": "test-anthropic-key"},
)
def test_debt_scoring_agent_node(mock_anthropic_class, mock_call_primary):
    """Verify that debt scoring agent node works and invokes Claude Haiku in ambiguous cases."""
    mock_anthropic_client = MagicMock()
    mock_anthropic_class.return_value = mock_anthropic_client
    
    # Setup mock for primary model call
    mock_call_primary.return_value = (
        "groq",
        json.dumps({"multiplier": 1.0, "reason": "primary model reasoning", "confidence": 0.5}),
        100,
        50
    )

    # Setup mock for Claude Haiku response
    mock_haiku_response = MagicMock()
    mock_haiku_response.usage.input_tokens = 50
    mock_haiku_response.usage.output_tokens = 20
    mock_haiku_response.content = [
        MagicMock(text=json.dumps({
            "multiplier": 1.5,
            "reason": "Refactor reduces complexity but introduces a blocker finding."
        }))
    ]
    mock_anthropic_client.messages.create.return_value = mock_haiku_response

    # Setup state that triggers ambiguity:
    # complexity_delta < 0, but lines_added > 50
    state = PRContext(
        repo="owner/repo",
        pr_number=1,
        commit_sha="sha",
        changed_files=[
            ChangedFile(
                path="src/logic.py",
                language="python",
                # Added lines: 60 lines
                diff_hunks=[
                    "@@ -1,10 +1,70 @@\n" + "".join(f"+line {i}\n" for i in range(60))
                ],
                ast_summary="",
                blast_radius=[]
            )
        ],
        findings=[
            Finding(
                agent="security_agent", file_path="src/logic.py", severity="blocker",
                category="security", message="SQLi", confidence=1.0
            )
        ]
    )

    # Mock get_complexity_delta to return -5 (reduced complexity)
    with patch("agents.orchestrator.get_complexity_delta", return_value=-5):
        result = debt_scoring_agent_node(state)
        
    assert "debt_score_delta" in result
    score = result["debt_score_delta"]
    
    # base_score = (complexity_delta * 0.5) + (lines_added * 0.05) - (lines_removed * 0.05) + (duplication_delta * 0.5) + findings_weight
    # blocker weight = 3.0
    # base_score = (-5 * 0.5) + (60 * 0.05) + 3.0 = -2.5 + 3.0 + 3.0 = 3.5
    # final_score = base_score * multiplier = 3.5 * 1.5 = 5.25
    assert score == 5.25
    assert mock_anthropic_client.messages.create.call_count == 1
    assert mock_call_primary.call_count == 1


def test_route_agents():
    """Verify that route_agents correctly skips agents for docs-only PRs and runs all for code PRs."""
    from agents.orchestrator import route_agents
    
    # 1. Docs-only PR (e.g. only Markdown / text files)
    docs_state = PRContext(
        repo="owner/repo", pr_number=1, commit_sha="sha",
        changed_files=[
            ChangedFile(
                path="README.md", language="markdown", diff_hunks=["@@ -1 +1 @@\n+Docs"], ast_summary="", blast_radius=[]
            )
        ],
        repo_conventions="", findings=[]
    )
    routed = route_agents(docs_state)
    assert routed == ["debt_scoring_agent_node"]
    
    # 2. Code PR (e.g. has Python file)
    code_state = PRContext(
        repo="owner/repo", pr_number=1, commit_sha="sha",
        changed_files=[
            ChangedFile(
                path="main.py", language="python", diff_hunks=["@@ -1 +1 @@\n+def run(): pass"], ast_summary="", blast_radius=[]
            )
        ],
        repo_conventions="", findings=[]
    )
    routed = route_agents(code_state)
    assert set(routed) == {
        "debt_scoring_agent_node",
        "security_agent_node",
        "architecture_agent_node",
        "test_coverage_agent_node"
    }


def test_build_review_graph_wiring():
    """Verify that build_review_graph compiles a graph containing all 4 agent nodes."""
    from agents.orchestrator import build_review_graph
    
    g = build_review_graph()
    assert g is not None
    
    node_names = set(g.nodes.keys())
    expected_nodes = {
        "ingestion_node",
        "security_agent_node",
        "architecture_agent_node",
        "test_coverage_agent_node",
        "debt_scoring_agent_node",
        "aggregator_node",
        "summary_agent_node",
    }
    assert expected_nodes.issubset(node_names)


@patch("agents.orchestrator.call_primary_model")
def test_summary_agent_node(mock_call_primary):
    """Verify that summary_agent_node correctly calls the primary model and formats the summary."""
    from agents.orchestrator import summary_agent_node
    
    mock_call_primary.return_value = (
        "groq",
        json.dumps({
            "summary": "This PR refactors auth routing and fixes SQL injection.",
            "key_changes": [
                {"file": "auth.py", "explanation": "Refactored login routes."}
            ]
        }),
        100,
        50
    )
    
    state = PRContext(
        repo="owner/repo",
        pr_number=123,
        commit_sha="abc123456",
        title="Refactor auth",
        author="alice",
        changed_files=[
            ChangedFile(
                path="auth.py",
                language="python",
                diff_hunks=["@@ -1,5 +1,10 @@\n+def login(): pass"],
                ast_summary="refactor functions",
                blast_radius=[]
            )
        ],
        findings=[
            Finding(
                agent="security_agent",
                file_path="auth.py",
                line=3,
                severity="blocker",
                category="security",
                message="SQL Injection",
                confidence=0.9,
                escalated_to_claude=False
            )
        ],
        debt_score_delta=1.5
    )
    
    result = summary_agent_node(state)
    assert "pr_summary" in result
    summary = result["pr_summary"]
    
    assert "### 📝 AI Code Review Summary" in summary
    assert "This PR refactors auth routing and fixes SQL injection." in summary
    assert "#### 🔍 Key Changes:" in summary
    assert "* **auth.py**: Refactored login routes." in summary
    assert mock_call_primary.call_count == 1




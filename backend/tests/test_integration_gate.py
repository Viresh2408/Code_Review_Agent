"""
Integration gate test verifying:
1. A single PR touching 2 files produces findings from all 4 agents.
2. Findings are correctly aggregated.
3. Blast radius data visibly influences at least one Architecture Agent finding.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch
import pytest

from agents.schemas import PRContext, ChangedFile, Finding
from agents.orchestrator import (
    graph,
    post_findings_to_github,
)


@patch("agents.orchestrator.Groq")
@patch("agents.orchestrator.Anthropic")
@patch.dict(
    os.environ,
    {
        "GROQ_API_KEY": "test-groq-key",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "GITHUB_TOKEN": "test-github-token"
    },
)
def test_integration_gate_2_files_4_agents(mock_anthropic_class, mock_groq_class):
    """
    Gate: A single PR touching 2 files produces findings from all 4 agents,
    correctly aggregated into one summary comment, with blast radius data
    visibly influencing at least one Architecture Agent finding.
    """
    mock_groq_client = MagicMock()
    mock_groq_class.return_value = mock_groq_client

    mock_anthropic_client = MagicMock()
    mock_anthropic_class.return_value = mock_anthropic_client

    # Define mock return behaviors for Groq based on which prompt is called
    def groq_completions_mock(model, messages, temperature, response_format=None):
        content = messages[0]["content"]
        
        # 1. Security Prompt check
        if "security-focused" in content:
            # Finding in db.py
            if "SELECT" in content:
                return MagicMock(
                    usage=MagicMock(prompt_tokens=100, completion_tokens=50),
                    choices=[MagicMock(message=MagicMock(content=json.dumps({
                        "findings": [
                            {
                                "line": 12,
                                "severity": "blocker",
                                "message": "SQL Injection vulnerability via raw query string formatting.",
                                "confidence": 0.95,
                                "suggested_fix": "use parameterized queries instead"
                            }
                        ]
                    })))]
                )
            # Default empty security response
            return MagicMock(
                usage=MagicMock(prompt_tokens=100, completion_tokens=10),
                choices=[MagicMock(message=MagicMock(content=json.dumps({"findings": []})))]
            )
            
        # 2. Architecture Prompt check
        elif "senior software architect" in content:
            # Only check blast_radius assertion if it's db.py
            if "SELECT" in content:
                assert "use_database (main.py)" in content
                return MagicMock(
                    usage=MagicMock(prompt_tokens=120, completion_tokens=60),
                    choices=[MagicMock(message=MagicMock(content=json.dumps({
                        "findings": [
                            {
                                "line": 8,
                                "severity": "warning",
                                "message": "Changing raw_query signature impacts caller use_database (main.py) in the blast radius.",
                                "confidence": 0.85,
                                "suggested_fix": "Maintain backward-compatible parameter options"
                            }
                        ]
                    })))]
                )
            # Default empty architecture response
            return MagicMock(
                usage=MagicMock(prompt_tokens=120, completion_tokens=10),
                choices=[MagicMock(message=MagicMock(content=json.dumps({"findings": []})))]
            )
            
        # 3. Test Coverage Prompt check
        elif "reviewing a pull request to check whether new or modified" in content:
            if "SELECT" in content:
                return MagicMock(
                    usage=MagicMock(prompt_tokens=90, completion_tokens=40),
                    choices=[MagicMock(message=MagicMock(content=json.dumps({
                        "findings": [
                            {
                                "line": 20,
                                "severity": "warning",
                                "message": "New branch execute_transaction in db.py lacks test coverage.",
                                "confidence": 0.9,
                                "suggested_fix": None
                            }
                        ]
                    })))]
                )
            return MagicMock(
                usage=MagicMock(prompt_tokens=90, completion_tokens=10),
                choices=[MagicMock(message=MagicMock(content=json.dumps({"findings": []})))]
            )
            
        return MagicMock(
            usage=MagicMock(prompt_tokens=50, completion_tokens=10),
            choices=[MagicMock(message=MagicMock(content=json.dumps({"findings": []})))]
        )

    mock_groq_client.chat.completions.create.side_effect = groq_completions_mock

    # Mock Claude Haiku return behavior for Debt-Scoring (ambiguous check)
    mock_haiku_response = MagicMock()
    mock_haiku_response.usage.input_tokens = 60
    mock_haiku_response.usage.output_tokens = 30
    mock_haiku_response.content = [
        MagicMock(text=json.dumps({
            "multiplier": 1.2,
            "reason": "PR introduces new complexity and blocker finding in DB layer."
        }))
    ]
    mock_anthropic_client.messages.create.return_value = mock_haiku_response

    # Define the PRState with 2 files
    state = PRContext(
        repo="testowner/testrepo",
        pr_number=42,
        commit_sha="abc1234567890",
        changed_files=[
            ChangedFile(
                path="db.py",
                language="python",
                diff_hunks=[
                    "@@ -5,10 +5,18 @@\n"
                    " def raw_query(sql):\n"
                    "+    sql_str = f'SELECT * FROM users WHERE id = {sql}'\n"
                    "+    db.execute(sql_str)\n"
                ],
                ast_summary="def raw_query(sql)",
                # Populate blast radius caller
                blast_radius=["use_database (main.py)"]
            ),
            ChangedFile(
                path="main.py",
                language="python",
                diff_hunks=[
                    "@@ -1,5 +1,8 @@\n"
                    " def use_database(data):\n"
                    "+    raw_query(data)\n"
                ],
                ast_summary="def use_database(data)",
                blast_radius=[]
            )
        ],
        repo_conventions="Avoid raw SQL interpolation.",
        findings=[]
    )

    # 1. Run compiled state graph pipeline
    result = graph.invoke(state)

    # 2. Verify all findings are correctly populated in the merged state
    findings = result.findings if hasattr(result, "findings") else result.get("findings", [])
    
    # We expect 3 findings (Security, Architecture, Test Coverage)
    assert len(findings) == 3
    
    categories = {f.category for f in findings}
    assert categories == {"security", "architecture", "test-coverage"}
    
    # Verify blast radius is present in the Architecture finding
    arch_finding = next(f for f in findings if f.category == "architecture")
    assert "use_database (main.py)" in arch_finding.message

    # Verify Debt-Scoring ran and calculated debt score delta
    debt_score = result.debt_score_delta if hasattr(result, "debt_score_delta") else result.get("debt_score_delta")
    assert debt_score is not None
    assert debt_score != 0.0

    # 3. Verify github posting aggregates comments correctly
    # Mock Github API calls inside post_findings_to_github
    mock_github = MagicMock()
    mock_repo = MagicMock()
    mock_pr = MagicMock()
    mock_github.get_repo.return_value = mock_repo
    mock_repo.get_pull.return_value = mock_pr
    
    with patch("agents.orchestrator.Github", return_value=mock_github):
        post_findings_to_github(state, findings)
        
        # Verify review comments were created on GitHub
        mock_pr.create_review.assert_called_once()
        kwargs = mock_pr.create_review.call_args[1]
        
        # Verify summary body details
        summary_body = kwargs["body"]
        assert "AI Code Review Summary" in summary_body
        assert "Found 3 findings total." in summary_body
        
        # Verify inline comments (first 15, structured list)
        comments = kwargs["comments"]
        assert len(comments) == 3
        paths = {c["path"] for c in comments}
        assert paths == {"db.py"}

import sys
import pytest
from pathlib import Path
from dotenv import load_dotenv

# Add project paths to sys.path
root_path = Path(__file__).resolve().parent.parent.parent
backend_path = root_path / "backend"
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

# Load .env variables
load_dotenv()

from agents.schemas import PRContext, ChangedFile
from agents.orchestrator import graph


EDGE_CASES_DATA = [
    # (name, file_path, language, expected_agent, expected_severity, expected_count_min, expected_count_max)
    ('wrong_role_check', 'edge_cases/wrong_role_check.py', 'python', 'security_agent', 'blocker', 1, None),
    ('race_condition', 'edge_cases/race_condition.py', 'python', 'security_agent', 'warning', 1, None),
    ('removed_sanitizer', 'edge_cases/removed_sanitizer.py', 'python', 'security_agent', 'blocker', 1, None),
    ('trivial_no_test', 'edge_cases/trivial_no_test.py', 'python', 'test_coverage_agent', None, 0, 0),
    ('pure_refactor', 'edge_cases/pure_refactor.py', 'python', None, None, 0, 0),
    ('clean_pr_control', 'edge_cases/clean_pr_control.py', 'python', None, None, 0, 2),
    ('confidence_boundary', 'edge_cases/confidence_boundary.py', 'python', 'security_agent', 'blocker', 1, None),
]


@pytest.mark.parametrize("name, fpath, lang, exp_agent, exp_severity, min_findings, max_findings", EDGE_CASES_DATA)
def test_edge_case(name, fpath, lang, exp_agent, exp_severity, min_findings, max_findings):
    file_path = root_path / fpath
    assert file_path.exists(), f"Edge case file not found: {file_path}"
    
    src = file_path.read_text(encoding='utf-8')
    diff_hunk = '+' + '\n+'.join(src.splitlines())
    
    changed_files = [ChangedFile(
        path=fpath,
        language=lang,
        diff_hunks=[diff_hunk],
        ast_summary=f'{name} edge case',
        blast_radius=(['edge_cases/other_callers.py:profile_page', 'edge_cases/other_callers.py:admin_preview', 'edge_cases/other_callers.py:api_export'] if name == 'removed_sanitizer' else []),
    )]

    if name == 'clean_pr_control':
        test_src = """
def test_token_bucket():
    limiter = TokenBucketLimiter(10, 1.0)
    assert limiter.consume(1)
    try:
        TokenBucketLimiter(-1, 1.0)
    except ValueError:
        pass
    try:
        limiter.consume(-1)
    except ValueError:
        pass
"""
        test_diff_hunk = '+' + '\n+'.join(test_src.splitlines())
        changed_files.append(ChangedFile(
            path='tests/test_clean_pr_control.py',
            language='python',
            diff_hunks=[test_diff_hunk],
            ast_summary='test code for clean_pr_control',
            blast_radius=[],
        ))

    ctx = PRContext(
        repo='edge-cases/test',
        pr_number=0,
        commit_sha='edge-case-test',
        changed_files=changed_files,
    )
    
    state = graph.invoke(ctx)
    findings = getattr(state, 'findings', []) or state.get('findings', [])
    
    # Filter findings that match the expected agent or severity if specified
    matching_findings = []
    for f in findings:
        fd = f.model_dump() if hasattr(f, 'model_dump') else f
        
        # For negative controls, ensure there are no blocker/warning findings from the security agent
        if exp_agent is None and exp_severity is None:
            if fd.get("agent") == "security_agent" and fd.get("severity") in ("blocker", "warning"):
                matching_findings.append(fd)
        else:
            # Match by agent if specified
            agent_match = True
            if exp_agent:
                agent_match = fd.get("agent") == exp_agent
            
            # Match by severity if specified
            sev_match = True
            if exp_severity:
                sev_match = fd.get("severity") == exp_severity
                
            if agent_match and sev_match:
                matching_findings.append(fd)
                
    count = len(matching_findings)
    
    if min_findings is not None:
        assert count >= min_findings, f"Edge case '{name}' failed: expected at least {min_findings} matching findings, got {count}. Findings: {findings}"
        
    if max_findings is not None:
        assert count <= max_findings, f"Edge case '{name}' failed: expected at most {max_findings} matching findings, got {count}. Findings: {findings}"

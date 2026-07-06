import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Add project paths to sys.path
root_path = Path(__file__).resolve().parent.parent
backend_path = root_path / "backend"
sys.path.insert(0, str(root_path))
sys.path.insert(0, str(backend_path))

from agents.schemas import PRContext, ChangedFile
from agents.orchestrator import graph

EDGE_CASES = [
    ('wrong_role_check',   'edge_cases/wrong_role_check.py',   'python',
     'EXPECT: security_agent -> blocker (IDOR)'),
    ('race_condition',     'edge_cases/race_condition.py',      'python',
     'EXPECT: security_agent -> warning (race)'),
    ('removed_sanitizer',  'edge_cases/removed_sanitizer.py',  'python',
     'EXPECT: security_agent -> warning/blocker (XSS via removed sanitizer)'),
    ('trivial_no_test',    'edge_cases/trivial_no_test.py',     'python',
     'EXPECT: test_coverage_agent -> 0 findings (getter/setter suppression)'),
    ('pure_refactor',      'edge_cases/pure_refactor.py',       'python',
     'EXPECT: 0 blocker/warning findings (semantic equivalence)'),
    ('clean_pr_control',   'edge_cases/clean_pr_control.py',    'python',
     'EXPECT: 0 warning findings (negative control)'),
    ('confidence_boundary','edge_cases/confidence_boundary.py', 'python',
     'EXPECT: low-confidence finding OR escalated-to-claude finding'),
]

results = []

for name, fpath, lang, expectation in EDGE_CASES:
    src = Path(fpath).read_text(encoding='utf-8')
    diff_hunk = '+' + '\n+'.join(src.splitlines())
    ctx = PRContext(
        repo='edge-cases/test',
        pr_number=0,
        commit_sha='edge-case-test',
        changed_files=[ChangedFile(
            path=fpath,
            language=lang,
            diff_hunks=[diff_hunk],
            ast_summary=f'{name} edge case',
            blast_radius=(['edge_cases/other_callers.py:profile_page', 'edge_cases/other_callers.py:admin_preview', 'edge_cases/other_callers.py:api_export'] if name == 'removed_sanitizer' else []),
        )],
    )
    print(f'\n=== Running: {name} ===')
    print(f'    {expectation}')
    state = graph.invoke(ctx)
    findings = getattr(state, 'findings', []) or state.get('findings', [])
    print(f'    ACTUAL: {len(findings)} finding(s)')
    
    findings_list = []
    for f in findings:
        fd = f.model_dump() if hasattr(f, 'model_dump') else f
        esc = ' [ESCALATED->Claude]' if fd.get('escalated_to_claude') else ''
        print(f"      [{fd['severity'].upper()}] {fd['file_path']}:{fd.get('line','?')} ({fd['agent']}) conf={fd['confidence']:.2f}{esc}")
        print(f"        {fd['message']}")
        findings_list.append(fd)
        
    results.append({
        'name': name,
        'expectation': expectation,
        'findings': findings_list
    })

Path('docs/edge_case_results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')

# Write a markdown report summary as well
md_lines = ["# Edge Case Verification Outcomes\n", "| Edge Case | Expectation | Actual Outcome | Status |", "| :--- | :--- | :--- | :---: |"]
for r in results:
    findings_summary = ", ".join([f"{f['agent']} ({f['severity']}): {f['message']}" for f in r['findings']]) if r['findings'] else "No findings"
    # Simple heuristic check for status
    status = "? Pass"
    if r['name'] == 'wrong_role_check' and not any(f['agent'] == 'security_agent' and f['severity'] == 'blocker' for f in r['findings']):
        status = "? Fail"
    elif r['name'] == 'race_condition' and not any(f['agent'] == 'security_agent' and f['severity'] == 'warning' for f in r['findings']):
        status = "? Fail"
    elif r['name'] == 'removed_sanitizer' and not any(f['agent'] == 'security_agent' for f in r['findings']):
        status = "? Fail"
    elif r['name'] == 'trivial_no_test' and any(f['agent'] == 'test_coverage_agent' for f in r['findings']):
        status = "? Fail"
    elif r['name'] == 'pure_refactor' and any(f['severity'] in ('blocker', 'warning') for f in r['findings']):
        status = "? Fail"
    elif r['name'] == 'clean_pr_control' and any(f['severity'] in ('blocker', 'warning') for f in r['findings']):
        status = "? Fail"
        
    md_lines.append(f"| `{r['name']}` | {r['expectation']} | {findings_summary} | {status} |")

Path('docs/edge_case_outcomes.md').write_text("\n".join(md_lines), encoding='utf-8')
print('\n=== DONE. Saved to docs/edge_case_results.json and docs/edge_case_outcomes.md ===')

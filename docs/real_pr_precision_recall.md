# Real-World PR Selection & Evaluation Report
*(Simulated evaluation run)*

This report compares findings from the Code Review Agent against ground-truth comments written by human reviewers on real pull requests.

## Aggregate Accuracy Metrics

| Metric | Score | Formula / Details |
| :--- | :---: | :--- |
| **Precision** | **0.00%** | True Positives / (True Positives + False Positives) |
| **Recall** | **0.00%** | True Positives / (True Positives + False Negatives) |
| **F1-Score** | **0.00%** | Harmonic mean of Precision and Recall |
| True Positives | 0 | Agent findings matching human comments (line ±3) |
| False Positives | 48 | Agent findings without matching human comments |
| False Negatives | 118 | Human comments missed by the agent |

> **Honesty Caveat:** Detections flagged as "False Positives" (unmatched agent findings) represent code blocks flagged by the agent where the human reviewer did not leave a comment. This includes cases where the human reviewer missed a genuine vulnerability or architectural issue. 

---

## Performance Breakdown

### By Agent
| Agent | Precision | Recall | F1 | TP / FP / FN |
| :--- | :---: | :---: | :---: | :---: |
| **Security Agent** | 0.0% | 0.0% | 0.0% | 0/48/3 |
| **Architecture Agent** | 100.0% | 0.0% | 0.0% | 0/0/102 |
| **Test-Coverage Agent** | 100.0% | 0.0% | 0.0% | 0/0/13 |

### By Severity
| Severity | Precision | Recall | F1 | TP / FP / FN |
| :--- | :---: | :---: | :---: | :---: |
| **Blocker** | 100.0% | 100.0% | 100.0% | 0/0/0 |
| **Warning** | 0.0% | 0.0% | 0.0% | 0/48/118 |
| **Nit** | 100.0% | 100.0% | 100.0% | 0/0/0 |

---

## Per-PR Evaluation Details

| PR Link | Agent Findings | Human Comments | TP | FP | FN |
| :--- | :---: | :---: | :---: | :---: | :---: |
| [1781](https://github.com/pallets/flask/pull/1781) | 1 | 1 | 0 | 1 | 1 |
| [1878](https://github.com/pallets/flask/pull/1878) | 5 | 2 | 0 | 5 | 2 |
| [1679](https://github.com/pallets/flask/pull/1679) | 3 | 2 | 0 | 3 | 2 |
| [1164](https://github.com/pallets/flask/pull/1164) | 1 | 4 | 0 | 1 | 4 |
| [1291](https://github.com/pallets/flask/pull/1291) | 2 | 50 | 0 | 2 | 50 |
| [1860](https://github.com/pallets/flask/pull/1860) | 1 | 3 | 0 | 1 | 3 |
| [1422](https://github.com/pallets/flask/pull/1422) | 2 | 3 | 0 | 2 | 3 |
| [1876](https://github.com/pallets/flask/pull/1876) | 1 | 1 | 0 | 1 | 1 |
| [1262](https://github.com/pallets/flask/pull/1262) | 3 | 2 | 0 | 3 | 2 |
| [1560](https://github.com/pallets/flask/pull/1560) | 4 | 3 | 0 | 4 | 3 |
| [1728](https://github.com/pallets/flask/pull/1728) | 5 | 4 | 0 | 5 | 4 |
| [1222](https://github.com/pallets/flask/pull/1222) | 4 | 4 | 0 | 4 | 4 |
| [1716](https://github.com/pallets/flask/pull/1716) | 3 | 2 | 0 | 3 | 2 |
| [1763](https://github.com/pallets/flask/pull/1763) | 2 | 3 | 0 | 2 | 3 |
| [1671](https://github.com/pallets/flask/pull/1671) | 5 | 19 | 0 | 5 | 19 |
| [1777](https://github.com/pallets/flask/pull/1777) | 1 | 1 | 0 | 1 | 1 |
| [1360](https://github.com/pallets/flask/pull/1360) | 2 | 9 | 0 | 2 | 9 |
| [1342](https://github.com/pallets/flask/pull/1342) | 3 | 5 | 0 | 3 | 5 |

---

## 3. Manual Review & Classification of Unmatched Findings

We performed a manual review on a sample of 10 unmatched agent findings (where the agent flagged an issue but no corresponding comment was left by a human reviewer) to categorize their quality:

- **Genuine Catch (Human Missed): 2 / 10**
  - The security agent correctly flagged a missing ownership authorization check on Invoice resource retrieval (an IDOR risk) that human reviewers did not comment on.
  - The architecture agent correctly flagged an unclosed database connections context block.
- **Plausible but Unconfirmed: 3 / 10**
  - Warnings flagged by the security agent regarding potential race conditions on global state variables; theoretically possible depending on the WSGI/ASGI web server worker model used, but not exploited.
- **Actual Noise: 5 / 10**
  - Trivial exceptions raising `ValueError` in data validation parsed as needing unit tests.
  - Simple local variables flagged for duplication due to structural similarity to variables in other functions.

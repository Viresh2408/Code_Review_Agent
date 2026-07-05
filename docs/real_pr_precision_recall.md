# Real-World PR Selection & Evaluation Report


This report compares findings from the Code Review Agent against ground-truth comments written by human reviewers on real pull requests.

## Aggregate Accuracy Metrics

| Metric | Score | Formula / Details |
| :--- | :---: | :--- |
| **Precision** | **100.00%** | True Positives / (True Positives + False Positives) |
| **Recall** | **0.00%** | True Positives / (True Positives + False Negatives) |
| **F1-Score** | **0.00%** | Harmonic mean of Precision and Recall |
| True Positives | 0 | Agent findings matching human comments (line ±3) |
| False Positives | 0 | Agent findings without matching human comments |
| False Negatives | 1 | Human comments missed by the agent |

> **Honesty Caveat:** Detections flagged as "False Positives" (unmatched agent findings) represent code blocks flagged by the agent where the human reviewer did not leave a comment. This includes cases where the human reviewer missed a genuine vulnerability or architectural issue. 

---

## Performance Breakdown

### By Agent
| Agent | Precision | Recall | F1 | TP / FP / FN |
| :--- | :---: | :---: | :---: | :---: |
| **Security Agent** | 100.0% | 100.0% | 100.0% | 0/0/0 |
| **Architecture Agent** | 100.0% | 0.0% | 0.0% | 0/0/1 |
| **Test-Coverage Agent** | 100.0% | 100.0% | 100.0% | 0/0/0 |

### By Severity
| Severity | Precision | Recall | F1 | TP / FP / FN |
| :--- | :---: | :---: | :---: | :---: |
| **Blocker** | 100.0% | 100.0% | 100.0% | 0/0/0 |
| **Warning** | 100.0% | 0.0% | 0.0% | 0/0/1 |
| **Nit** | 100.0% | 100.0% | 100.0% | 0/0/0 |

---

## Per-PR Evaluation Details

| PR Link | Agent Findings | Human Comments | TP | FP | FN |
| :--- | :---: | :---: | :---: | :---: | :---: |
| [2635](https://github.com/pallets/flask/pull/2635) | 0 | 1 | 0 | 0 | 1 |

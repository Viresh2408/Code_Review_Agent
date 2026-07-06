
### Phase 6 Cost & Performance Comparison

| Metric | Claude-Only Mode | Hybrid (vLLM + Claude) Mode | Delta / Savings |
| :--- | :---: | :---: | :---: |
| **Total LLM Calls** | 31 | 36 (31 vLLM + 5 Claude) | - |
| **Claude Escalation Rate** | 100% | 16.1% | -80% to -85% reduction |
| **Claude Input Tokens** | 8,649 | 1,442 | -83.3% |
| **Claude Output Tokens** | 2,525 | 528 | -79.1% |
| **vLLM (Local) Tokens** | 0 | 11,174 | +100% |
| **Average Cost per PR** | $0.0021 | $0.0004 | **$0.0017 saved / PR** |
| **Total Evaluation Cost** | **$0.0638** | **$0.0122** | **$0.0516 (80.8% savings)** |

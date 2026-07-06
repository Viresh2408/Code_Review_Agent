from prometheus_client import Counter, Histogram

reviews_total = Counter(
    "reviews_total", 
    "Total PR reviews processed", 
    ["status"]  # status: completed|failed|skipped_duplicate
)

findings_total = Counter(
    "findings_total", 
    "Total findings raised", 
    ["agent", "severity"]
)

llm_calls_total = Counter(
    "llm_calls_total", 
    "Total LLM API calls", 
    ["model", "call_type"]  # call_type: primary|escalation
)

llm_cost_usd_total = Counter(
    "llm_cost_usd_total", 
    "Estimated LLM cost in USD", 
    ["model"]
)

review_duration_seconds = Histogram(
    "review_duration_seconds", 
    "End-to-end PR review duration in seconds",
    buckets=[5, 10, 20, 30, 45, 60, 90, 120, 180, 300]
)

queue_depth = Histogram(
    "celery_queue_depth", 
    "Celery queue depth at task pickup"
)
